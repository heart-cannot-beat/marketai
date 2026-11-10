"""Meta observation batches into Scale source manifests (#109, #101 S3).

One connection's observation batch for target day D (``app.meta.
observation_sync``, #107/#108) reads every query instance of the connection's
Scale read contracts. When that batch Job reaches a terminal state -- every
run succeeded, or its last attempt failed the rest -- the Worker enqueues one
``scale_collect_sources`` Job (:func:`enqueue_source_collection`, key
``scale-collect:{connection_id}:{D}:{run_generation}``). The Job
(:func:`run_source_collection`) turns the batch into one immutable
``ScaleEvidenceSourceManifestRecord`` per ``active`` Enrollment's current
Episode on the connection, each in its own short transaction
(:func:`collect_episode_sources`):

1. lock the Episode; a manifest already collected under the same key is
   returned as it is (a replay allocates no revision);
2. check the connection, its binding and the Episode's contract set, then
   the runs, pages, call intents and attestations of every query instance
   the set requires, and parse the pages with the pure parsers of
   ``app.meta.scale_read_parsers``;
3. allocate the next source revision, write the source records (daily slots
   only from the Episode's ``started_local_day`` on), the manifest items and
   the source quality issues, and the manifest with its canonical hash;
4. advance the Episode's evidence-readiness projection by source revision.

A manifest is ``complete`` when every required instance has a succeeded run
with a contiguous page chain, no unreconciled uncertain call intent, a valid
attestation, a generation matching the Episode's contract set, and pages
that parse for the Episode's account, currency, time zone and ad. A day of a
complete daily read without a row for the ad is observed zero delivery
(#101 Q7). Otherwise it is ``blocked`` with the spec §1.7 floor codes and the
recovery actions of #101 §3:

========================== =========================================== ======================
code                       when                                        recovery action
========================== =========================================== ======================
transport_exhausted        a run ended other than ``succeeded``        wait_for_next_sync
incomplete_pagination      the page chain is broken                    retry_meta_sync
reconciliation_unresolved  a dispatched intent has no outcome and no   reconcile_meta_sync
                           confirmed reconciliation
contract_instance_failed   no run, another generation or hash, no      wait_for_next_sync
                           valid attestation, pages that break the
                           parser rules
source_provenance_mismatch account, time zone or currency differs      repair_connection
                           from the Episode, or the ad does not
                           resolve to exactly one configuration
connection_ineligible      the connection or its binding is not active repair_connection
========================== =========================================== ======================

A blocked manifest writes no source record, so it cannot be sealed: it only
moves the readiness projection to maturity ``insufficient``, quality
``blocked`` and its recovery action (spec §8). A complete manifest still
carries typed quality issues: an exact window without delivery has no
frequency (``exact_window_value_unavailable``), and an instance whose run
finished more than the contract's 36 hours before the batch's last run is
``freshness_slo_exceeded`` (contract §8; the Policy is chosen when S4 builds
the run, so the collector uses the contract's SLO).

The delivery-configuration source records the ad's status history -- the
campaign, ad set and ad effective statuses of every Episode day in the
lookback whose configuration read succeeded -- and the delivery diagnostic
Meta returned (``learning_stage_info``), both sealed into snapshot schema v3
(``app.scale.evidence``).

A complete manifest also requests the Episode's ``live`` evaluation run and
enqueues ``scale_evaluate`` in the same transaction
(:func:`app.scale.live_trigger.trigger_live_evaluation`, #110): the
manifest, the run and the Job commit together, and a replayed collection,
which finds the manifest by its key, triggers nothing again. The fallback
Advice-expiry sweep of #111 runs in :func:`run_source_collection` after the
Episode loop, in its own transaction.

ADR 0017: this module reads Meta rows and parses stored pages; it imports
neither ``app.meta.credential_resolution`` nor ``app.adapters.meta``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, new_id, utcnow
from app.meta.models import (
    EvidenceAttestationRecord,
    EvidencePublicationRecord,
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaCallReconciliationRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
    MetaEvidencePageRecord,
    MetaObservationSyncRunRecord,
    MetaPageFetchAttemptRecord,
    ObservationContractGenerationRecord,
)
from app.meta.observation import observation_read_plan, occurrence_id
from app.meta.scale_read_contracts import (
    CONFIGURATION_READ,
    DAILY,
    DELIVERY_CONFIGURATION,
    EXACT_WINDOW,
    SCALE_READ_POLICY,
    AccountBinding,
    ScaleReadContractError,
    contract_for,
)
from app.meta.scale_read_parsers import (
    DayObservation,
    DeliveryConfigurationObservation,
    ScaleReadParseError,
    WindowObservation,
    parse_daily,
    parse_delivery_configuration,
    parse_window,
)
from app.platform.audit import append_event
from app.platform.jobs import enqueue_in_session
from app.platform.runtime_role import WORKER_ROLE, runs_as
from app.platform.tenancy import VALID_META_MODES, TenantContext
from app.scale._canonical import canonical_hash
from app.scale.advice import expire_overdue_connection_advice
from app.scale.live_trigger import trigger_live_evaluation
from app.scale.models import (
    ScaleBreakdownSourceRecord,
    ScaleDailyMetricSourceRecord,
    ScaleDeliveryConfigSourceRecord,
    ScaleEnrollmentRecord,
    ScaleEpisodeProjectionRecord,
    ScaleEpisodeRecord,
    ScaleEvidenceReadinessProjectionRecord,
    ScaleEvidenceSourceManifestRecord,
    ScaleExactWindowSourceRecord,
    ScaleManifestItemRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScaleSourceQualityIssueRecord,
)

__all__ = [
    "COLLECT_JOB_TYPE",
    "BLOCKED_CODES",
    "RECOVERY_ACTIONS",
    "SourceCollectionError",
    "SourceCollectionFailed",
    "CollectedManifest",
    "CollectionResult",
    "CollectionJobInput",
    "collection_key",
    "collection_job_context",
    "collection_job_input",
    "enqueue_source_collection",
    "run_source_collection",
    "collect_episode_sources",
]

COLLECT_JOB_TYPE = "scale_collect_sources"
COLLECTOR_EXECUTOR = "worker:scale-collect-sources"
MANIFEST_HASH_DOMAIN = "scale-source-manifest-v1"
REQUEST_SET_HASH_DOMAIN = "scale-source-request-set-v1"
RUN_EVIDENCE_HASH_DOMAIN = "scale-sync-run-evidence-v1"
DELIVERY_EVIDENCE_HASH_DOMAIN = "scale-delivery-evidence-v1"
DELIVERY_COMPARABILITY_HASH_DOMAIN = "scale-delivery-comparability-v1"
#: Daily and configuration reads look back 14 local days ending with D.
LOOKBACK_DAYS = 14
#: The data-continuity generation of a collected manifest: one per Episode
#: until a semantically compatible generation change exists (S7).
SOURCE_GENERATION = 1
#: The scheduled batch reads run generation 1 (#107).
DEFAULT_RUN_GENERATION = 1

TRANSPORT_EXHAUSTED = "transport_exhausted"
INCOMPLETE_PAGINATION = "incomplete_pagination"
RECONCILIATION_UNRESOLVED = "reconciliation_unresolved"
CONTRACT_INSTANCE_FAILED = "contract_instance_failed"
SOURCE_PROVENANCE_MISMATCH = "source_provenance_mismatch"
CONNECTION_INELIGIBLE = "connection_ineligible"
FRESHNESS_SLO_EXCEEDED = "freshness_slo_exceeded"
EXACT_WINDOW_VALUE_UNAVAILABLE = "exact_window_value_unavailable"

#: Spec §1.7's floor of terminal-source codes: any of them blocks a manifest.
BLOCKED_CODES = frozenset({
    TRANSPORT_EXHAUSTED, INCOMPLETE_PAGINATION, RECONCILIATION_UNRESOLVED,
    CONTRACT_INSTANCE_FAILED, SOURCE_PROVENANCE_MISMATCH, CONNECTION_INELIGIBLE,
})
#: The typed recovery action of each code (#101 §3). The readiness
#: projection shows the first one in this order that the manifest carries.
RECOVERY_ACTIONS: dict[str, str] = {
    CONNECTION_INELIGIBLE: "repair_connection",
    SOURCE_PROVENANCE_MISMATCH: "repair_connection",
    CONTRACT_INSTANCE_FAILED: "wait_for_next_sync",
    RECONCILIATION_UNRESOLVED: "reconcile_meta_sync",
    INCOMPLETE_PAGINATION: "retry_meta_sync",
    TRANSPORT_EXHAUSTED: "wait_for_next_sync",
    FRESHNESS_SLO_EXCEEDED: "wait_for_next_sync",
    EXACT_WINDOW_VALUE_UNAVAILABLE: "wait_for_next_sync",
}
#: Parser refusals that say the evidence is not this Episode's account,
#: currency, time zone or ad (contract §8); every other refusal breaks the
#: contract's parsing rules.
_PROVENANCE_REASONS = frozenset({"account_mismatch", "currency_mismatch", "timezone_mismatch", "ad_not_found"})
_RECONCILED = ("confirmed_succeeded", "confirmed_failed")
#: ``learning_stage_info.status`` as the parser reports it; ``unknown`` means
#: Meta did not return the field (contract §19).
_DIAGNOSTIC_FIELD = "adsets.learning_stage_info.status"
#: Currencies whose Meta budgets are already in major units (ISO 4217
#: exponent 0); every other currency Meta budgets in hundredths.
_ZERO_DECIMAL_CURRENCIES = frozenset({
    "BIF", "CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "UGX", "VND",
})


class SourceCollectionError(RuntimeError):
    """A collection Job that cannot run as given (``code`` is stable)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class SourceCollectionFailed(RuntimeError):
    """Some Episodes could not be collected; a retry collects them, and the
    ones already written are found again by their collection key."""

    def __init__(self, failures: Mapping[str, str]) -> None:
        self.failures = dict(failures)
        detail = "; ".join(f"{key}: {reason}" for key, reason in sorted(self.failures.items()))
        super().__init__(f"{len(self.failures)} Episode(s) not collected: {detail}")


@dataclass(frozen=True)
class CollectedManifest:
    enrollment_id: str
    episode_id: str
    manifest_id: str
    state: str
    source_revision: int
    replayed: bool
    #: The live run a complete manifest requested, or why it requested none
    #: (``app.scale.live_trigger``); both ``None`` on a replay.
    evaluation_run_id: str | None = None
    evaluation_skipped: str | None = None


@dataclass(frozen=True)
class CollectionResult:
    manifests: tuple[CollectedManifest, ...]
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Issue:
    code: str
    blocking: bool
    contract_type: str | None = None
    window_role: str | None = None
    local_day: date | None = None
    detail: str | None = None

    @property
    def recovery_action(self) -> str | None:
        return RECOVERY_ACTIONS.get(self.code)


def _blocked(code: str, detail: str, contract_type: str | None = None, window_role: str | None = None) -> _Issue:
    return _Issue(code, True, contract_type, window_role, None, detail[:500])


@dataclass(frozen=True)
class _RunEvidence:
    """One sync run's stored evidence, read once per collection Job."""

    run_id: str
    occurrence_id: str
    state: str
    completed_at: datetime | None
    issue: _Issue | None = None
    bodies: tuple[Mapping[str, Any], ...] = ()
    edge_pages: tuple[tuple[str, Mapping[str, Any]], ...] = ()
    evidence_hash: str = ""
    terminal_intent_id: str = ""
    terminal_page_id: str | None = None
    reconciliation_id: str | None = None
    attestation_id: str | None = None


@dataclass
class _Assessment:
    """Everything a manifest is built from, before anything is written."""

    request_set: list[dict[str, Any]] = field(default_factory=list)
    requested_types: list[str] = field(default_factory=list)
    issues: list[_Issue] = field(default_factory=list)
    runs: list[_RunEvidence] = field(default_factory=list)
    daily: tuple[_RunEvidence, str, tuple[DayObservation, ...]] | None = None
    windows: dict[str, tuple[_RunEvidence, str, WindowObservation]] = field(default_factory=dict)
    configuration: tuple[_RunEvidence, str, DeliveryConfigurationObservation] | None = None
    status_history: list[dict[str, str]] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(issue.code in BLOCKED_CODES for issue in self.issues)


# ---------------------------------------------------------------------------
# Keys, payloads and enqueueing
# ---------------------------------------------------------------------------


def collection_key(connection_id: str, target_date: date, run_generation: int) -> str:
    """The batch a manifest is collected from; also the collection Job's key."""
    return f"scale-collect:{connection_id}:{target_date.isoformat()}:{run_generation}"


def _require_mode(context: TenantContext) -> str:
    if context.meta_mode not in VALID_META_MODES:
        raise SourceCollectionError("meta_mode_required", "a collection runs in one Meta mode")
    return context.meta_mode


def collection_job_context(claimed: TenantContext, payload: Mapping[str, Any]) -> TenantContext:
    """The claimed Job's context with the payload's Meta mode.

    The Job row records no mode, and RLS shows Scale and Meta rows only to a
    context that names one; a payload without a valid mode cannot run.
    """
    mode = str(payload.get("meta_mode") or "")
    if mode not in VALID_META_MODES:
        raise SourceCollectionError("missing_input", "missing or invalid meta_mode")
    return replace(claimed, meta_mode=mode)


@dataclass(frozen=True)
class CollectionJobInput:
    context: TenantContext
    connection_id: str
    target_date: date
    run_generation: int


def collection_job_input(claimed: TenantContext, payload: Mapping[str, Any]) -> CollectionJobInput:
    """What a ``scale_collect_sources`` Job asks for, or ``missing_input``."""
    context = collection_job_context(claimed, payload)
    connection_id = str(payload.get("connection_id") or "")
    if not connection_id:
        raise SourceCollectionError("missing_input", "缺少 connection_id")
    try:
        target_date = date.fromisoformat(str(payload.get("target_date") or ""))
        run_generation = int(payload.get("run_generation") or DEFAULT_RUN_GENERATION)
    except (TypeError, ValueError) as exc:
        raise SourceCollectionError("missing_input", str(exc)) from exc
    if run_generation < 1:
        raise SourceCollectionError("missing_input", f"run_generation {run_generation} is not positive")
    return CollectionJobInput(context, connection_id, target_date, run_generation)


def enqueue_source_collection(
    session: Session,
    context: TenantContext,
    *,
    connection_id: str,
    target_date: date,
    run_generation: int = DEFAULT_RUN_GENERATION,
) -> JobRecord | None:
    """The collection Job for one finished observation batch, or ``None``.

    Called when the connection's batch Job for D reaches a terminal state,
    whether its runs succeeded or its last attempt failed them (a failed
    batch makes a blocked manifest). A connection without an ``active`` or
    ``paused`` Enrollment in this mode has nothing for Scale to collect and
    gets no Job; a paused Enrollment still gets one, because #111's fallback
    Advice-expiry sweep runs in it. The key names the batch, so a replayed
    batch names the same Job.
    """
    mode = _require_mode(context)
    enrolled = session.scalar(
        select(ScaleEnrollmentRecord.id).where(
            ScaleEnrollmentRecord.workspace_id == context.workspace_id,
            ScaleEnrollmentRecord.meta_mode == mode,
            ScaleEnrollmentRecord.meta_connection_id == connection_id,
            ScaleEnrollmentRecord.lifecycle.in_(("active", "paused")),
        ).limit(1)
    )
    if enrolled is None:
        return None
    return enqueue_in_session(
        session,
        context=context,
        job_type=COLLECT_JOB_TYPE,
        payload={
            "connection_id": connection_id,
            "meta_mode": mode,
            "target_date": target_date.isoformat(),
            "run_generation": run_generation,
        },
        idempotency_key=collection_key(connection_id, target_date, run_generation),
        resource_type="meta_connection",
        resource_id=connection_id,
    )


# ---------------------------------------------------------------------------
# The Job
# ---------------------------------------------------------------------------


@runs_as(WORKER_ROLE)
def run_source_collection(
    context: TenantContext,
    *,
    connection_id: str,
    target_date: date,
    run_generation: int = DEFAULT_RUN_GENERATION,
) -> CollectionResult:
    """Collect D's batch into a manifest for every active Enrollment on the connection.

    Each Episode commits on its own, so one that fails does not hold back
    the others; :class:`SourceCollectionFailed` then asks for a retry, which
    finds the manifests already written by their collection key.
    """
    mode = _require_mode(context)
    with tenant_session(context) as session:
        enrollment_ids = list(session.scalars(
            select(ScaleEnrollmentRecord.id)
            .where(
                ScaleEnrollmentRecord.workspace_id == context.workspace_id,
                ScaleEnrollmentRecord.meta_mode == mode,
                ScaleEnrollmentRecord.meta_connection_id == connection_id,
                ScaleEnrollmentRecord.lifecycle == "active",
                ScaleEnrollmentRecord.current_episode_id.is_not(None),
            )
            .order_by(ScaleEnrollmentRecord.id)
        ))
    cache: dict[str, _RunEvidence] = {}
    manifests: list[CollectedManifest] = []
    skipped: list[str] = []
    failures: dict[str, str] = {}
    for enrollment_id in enrollment_ids:
        try:
            with tenant_session(context) as session:
                collected = collect_episode_sources(
                    session, context, enrollment_id=enrollment_id, connection_id=connection_id,
                    target_date=target_date, run_generation=run_generation, cache=cache,
                )
        except Exception as exc:  # noqa: BLE001 -- one Episode must not stop the others
            failures[enrollment_id] = f"{type(exc).__name__}: {exc}"[:500]
            continue
        if collected is None:
            skipped.append(enrollment_id)
        else:
            manifests.append(collected)
    # #111 hook: the fallback Advice-expiry sweep belongs here, after the
    # Episode loop and in its own tenant transaction, so it also runs for a
    # batch that failed (it still enqueues this Job) and for a connection
    # whose Enrollments are all paused (the Job is enqueued for them too).
    expire_overdue_connection_advice(context, connection_id)
    if failures:
        raise SourceCollectionFailed(failures)
    return CollectionResult(manifests=tuple(manifests), skipped=tuple(skipped))


def collect_episode_sources(
    session: Session,
    context: TenantContext,
    *,
    enrollment_id: str,
    connection_id: str,
    target_date: date,
    run_generation: int = DEFAULT_RUN_GENERATION,
    cache: dict[str, _RunEvidence] | None = None,
) -> CollectedManifest | None:
    """Write the manifest of one Enrollment's current Episode for D, or find it.

    Runs in the caller's transaction (``session`` is bound to ``context``).
    Returns ``None`` when there is nothing to collect: the Enrollment is not
    active on this connection, its Episode is closed, or D precedes the
    Episode.
    """
    mode = _require_mode(context)
    cache = {} if cache is None else cache
    enrollment = session.scalar(
        select(ScaleEnrollmentRecord).where(
            ScaleEnrollmentRecord.workspace_id == context.workspace_id,
            ScaleEnrollmentRecord.meta_mode == mode,
            ScaleEnrollmentRecord.id == enrollment_id,
        )
    )
    if (
        enrollment is None or enrollment.lifecycle != "active"
        or enrollment.meta_connection_id != connection_id or enrollment.current_episode_id is None
    ):
        return None
    episode = session.scalar(
        select(ScaleEpisodeRecord)
        .where(
            ScaleEpisodeRecord.workspace_id == context.workspace_id,
            ScaleEpisodeRecord.meta_mode == mode,
            ScaleEpisodeRecord.id == enrollment.current_episode_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if episode is None or episode.phase == "closed" or episode.meta_connection_id != connection_id:
        return None
    key = collection_key(connection_id, target_date, run_generation)
    existing = _manifest_by_key(session, context, episode.id, key)
    if existing is not None:
        return _collected(enrollment.id, existing, replayed=True)
    if target_date < episode.started_local_day:
        return None

    assessment = _assess(session, context, episode, connection_id, target_date, run_generation, cache)
    trigger = None
    try:
        # One unit (#110): the manifest and, when it is complete, its live run
        # and Job commit together or not at all.
        with session.begin_nested():
            manifest = _write(session, context, episode, assessment, target_date, key)
            if manifest.state == "complete":
                trigger = trigger_live_evaluation(
                    session, context, enrollment=enrollment, episode=episode, manifest=manifest,
                    triggering_sync_run_id=_last_run_id(assessment),
                )
    except IntegrityError:
        # A concurrent collection of the same batch committed first.
        existing = _manifest_by_key(session, context, episode.id, key)
        if existing is None:
            raise
        return _collected(enrollment.id, existing, replayed=True)
    return _collected(enrollment.id, manifest, replayed=False,
                      evaluation_run_id=trigger.run_id if trigger else None,
                      evaluation_skipped=trigger.skipped if trigger else None)


def _collected(
    enrollment_id: str,
    manifest: ScaleEvidenceSourceManifestRecord,
    *,
    replayed: bool,
    evaluation_run_id: str | None = None,
    evaluation_skipped: str | None = None,
) -> CollectedManifest:
    return CollectedManifest(
        enrollment_id=enrollment_id,
        episode_id=manifest.episode_id,
        manifest_id=manifest.id,
        state=manifest.state,
        source_revision=manifest.source_revision,
        replayed=replayed,
        evaluation_run_id=evaluation_run_id,
        evaluation_skipped=evaluation_skipped,
    )


def _last_run_id(found: _Assessment) -> str | None:
    """The sync run whose completion is the manifest's ``terminal_at``: the
    run that triggered the evaluation (spec §3.8)."""
    finished = [(run.completed_at, run.run_id) for run in found.runs if run.completed_at is not None]
    return max(finished)[1] if finished else None


def _manifest_by_key(
    session: Session, context: TenantContext, episode_id: str, key: str,
) -> ScaleEvidenceSourceManifestRecord | None:
    return session.scalar(
        select(ScaleEvidenceSourceManifestRecord).where(
            ScaleEvidenceSourceManifestRecord.workspace_id == context.workspace_id,
            ScaleEvidenceSourceManifestRecord.meta_mode == context.meta_mode,
            ScaleEvidenceSourceManifestRecord.episode_id == episode_id,
            ScaleEvidenceSourceManifestRecord.collection_key == key,
        )
    )


# ---------------------------------------------------------------------------
# Assessment: what the batch proves for this Episode
# ---------------------------------------------------------------------------


def _as_utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _episode_days(episode: ScaleEpisodeRecord, target_date: date) -> list[date]:
    """The lookback's days that belong to the Episode: [max(D-13, start), D]."""
    first = max(target_date - timedelta(days=LOOKBACK_DAYS - 1), episode.started_local_day)
    return [first + timedelta(days=offset) for offset in range((target_date - first).days + 1)]


def _assess(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    connection_id: str,
    target_date: date,
    run_generation: int,
    cache: dict[str, _RunEvidence],
) -> _Assessment:
    found = _Assessment()
    found.issues.extend(_connection_issues(session, context, episode, connection_id))
    if not episode.ad_remote_id:
        found.issues.append(_blocked(SOURCE_PROVENANCE_MISMATCH, "the Episode binds no ad"))

    members = list(session.scalars(
        select(ScaleMetricSemanticContractSetMemberRecord)
        .where(ScaleMetricSemanticContractSetMemberRecord.contract_set_id == episode.metric_semantic_contract_set_id)
        .order_by(ScaleMetricSemanticContractSetMemberRecord.contract_type)
    ))
    absent = [kind for kind in SCALE_READ_POLICY.required_contract_types()
              if kind not in {member.contract_type for member in members}]
    if absent:
        found.issues.append(_blocked(
            CONTRACT_INSTANCE_FAILED, f"the Episode's contract set lacks {', '.join(absent)}",
        ))
    for member in members:
        found.requested_types.append(member.contract_type)
        found.request_set.append({
            "contract_type": member.contract_type,
            "generation_id": member.observation_contract_generation,
            "contract_hash": member.contract_hash,
        })
        resolved = _member_binding(session, context, episode, connection_id, member, found)
        if resolved is None:
            continue
        generation_id, binding, plan = resolved
        for instance in plan.instances:
            run = _run_for(session, context, generation_id, target_date, instance.name, run_generation)
            if run is None:
                found.issues.append(_blocked(
                    CONTRACT_INSTANCE_FAILED,
                    f"no sync run for {occurrence_id(generation_id, target_date, instance.name)}",
                    member.contract_type, instance.window_role,
                ))
                continue
            evidence = _run_evidence(session, context, run, cache)
            found.runs.append(evidence)
            if evidence.issue is not None:
                found.issues.append(replace(
                    evidence.issue, contract_type=member.contract_type, window_role=instance.window_role,
                ))
                continue
            try:
                _parse_instance(found, episode, member.contract_type, instance, target_date, binding, evidence)
            except ScaleReadParseError as exc:
                code = SOURCE_PROVENANCE_MISMATCH if exc.reason in _PROVENANCE_REASONS else CONTRACT_INSTANCE_FAILED
                found.issues.append(_blocked(code, f"{exc.reason}: {exc.detail}", member.contract_type,
                                             instance.window_role))
        if member.contract_type == DELIVERY_CONFIGURATION and found.configuration is not None:
            found.status_history = _status_history(
                session, context, episode, generation_id, target_date, binding, found.configuration[2], cache,
            )
    found.issues.extend(_freshness_issues(found))
    return found


def _connection_issues(
    session: Session, context: TenantContext, episode: ScaleEpisodeRecord, connection_id: str,
) -> list[_Issue]:
    connection = session.scalar(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.workspace_id == context.workspace_id,
            MetaConnectionRecord.meta_mode == context.meta_mode,
            MetaConnectionRecord.id == connection_id,
        )
    )
    if connection is None or connection.state != "active":
        state = "missing" if connection is None else connection.state
        return [_blocked(CONNECTION_INELIGIBLE, f"the connection is {state}")]
    binding = session.scalar(
        select(MetaDataSourceBindingRecord.id).where(
            MetaDataSourceBindingRecord.workspace_id == context.workspace_id,
            MetaDataSourceBindingRecord.meta_mode == context.meta_mode,
            MetaDataSourceBindingRecord.meta_connection_id == connection_id,
            MetaDataSourceBindingRecord.ad_account_id == episode.ad_account_id,
            MetaDataSourceBindingRecord.state == "active",
        ).limit(1)
    )
    if binding is None:
        return [_blocked(CONNECTION_INELIGIBLE, "the account has no active binding on the connection")]
    return []


def _member_binding(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    connection_id: str,
    member: ScaleMetricSemanticContractSetMemberRecord,
    found: _Assessment,
):
    """The member's generation, account binding and read plan, or ``None``
    after recording why its instances cannot be evidence."""
    kind = member.contract_type
    generation = session.scalar(
        select(ObservationContractGenerationRecord).where(
            ObservationContractGenerationRecord.workspace_id == context.workspace_id,
            ObservationContractGenerationRecord.meta_mode == context.meta_mode,
            ObservationContractGenerationRecord.id == member.observation_contract_generation,
        )
    )
    if generation is None:
        found.issues.append(_blocked(CONTRACT_INSTANCE_FAILED, "the member's generation does not exist", kind))
        return None
    if generation.meta_connection_id != connection_id or generation.ad_account_id != episode.ad_account_id:
        found.issues.append(_blocked(SOURCE_PROVENANCE_MISMATCH, "the generation reads another connection or account",
                                     kind))
        return None
    if generation.contract_hash != member.contract_hash:
        found.issues.append(_blocked(CONTRACT_INSTANCE_FAILED, "the generation's hash differs from the set's", kind))
        return None
    try:
        plan = observation_read_plan(generation)
    except ScaleReadContractError as exc:
        found.issues.append(_blocked(CONTRACT_INSTANCE_FAILED, f"{exc.code}: {exc}", kind))
        return None
    if plan is None or plan.contract is None or plan.binding is None or plan.contract.contract_type != kind:
        found.issues.append(_blocked(CONTRACT_INSTANCE_FAILED, "the generation is not read for this contract", kind))
        return None
    binding = plan.binding
    mismatches = [
        f"{name} {actual!r}, Episode {wanted!r}"
        for name, actual, wanted in (
            ("account currency", binding.currency, episode.currency),
            ("account time zone", binding.timezone_name, episode.timezone_name),
        )
        if actual != wanted
    ]
    if mismatches:
        # Contract §7: a cross-currency Episode cannot aggregate, and Scale
        # never converts. The Episode currency is the Workspace default
        # (enrollment.py); the account's is frozen in the contract (#108).
        found.issues.append(_blocked(SOURCE_PROVENANCE_MISMATCH, "; ".join(mismatches), kind))
        return None
    return generation.id, binding, plan


def _run_for(
    session: Session, context: TenantContext, generation_id: str, target_date: date, instance: str,
    run_generation: int,
) -> MetaObservationSyncRunRecord | None:
    return session.scalar(
        select(MetaObservationSyncRunRecord).where(
            MetaObservationSyncRunRecord.workspace_id == context.workspace_id,
            MetaObservationSyncRunRecord.meta_mode == context.meta_mode,
            MetaObservationSyncRunRecord.contract_generation_id == generation_id,
            MetaObservationSyncRunRecord.occurrence_id == occurrence_id(generation_id, target_date, instance),
            MetaObservationSyncRunRecord.run_generation == run_generation,
        )
    )


def _run_evidence(
    session: Session, context: TenantContext, run: MetaObservationSyncRunRecord, cache: dict[str, _RunEvidence],
) -> _RunEvidence:
    cached = cache.get(run.id)
    if cached is not None and cached.state == run.state:
        return cached
    evidence = _load_run_evidence(session, context, run)
    cache[run.id] = evidence
    return evidence


def _load_run_evidence(
    session: Session, context: TenantContext, run: MetaObservationSyncRunRecord,
) -> _RunEvidence:
    """The run's pages, checked: a contiguous chain to one terminal page, no
    dispatched call left without an outcome or a confirmed reconciliation,
    and a valid attestation of its publication."""
    base = _RunEvidence(
        run_id=run.id, occurrence_id=run.occurrence_id, state=run.state, completed_at=_as_utc(run.completed_at),
    )
    if run.state != "succeeded":
        return replace(base, issue=_blocked(TRANSPORT_EXHAUSTED, f"sync run {run.id} is {run.state}"))
    pages = list(session.scalars(
        select(MetaEvidencePageRecord)
        .where(
            MetaEvidencePageRecord.workspace_id == context.workspace_id,
            MetaEvidencePageRecord.meta_mode == context.meta_mode,
            MetaEvidencePageRecord.sync_run_id == run.id,
        )
        .order_by(MetaEvidencePageRecord.page_order)
    ))
    broken = _page_chain_break(run, pages)
    if broken:
        return replace(base, issue=_blocked(INCOMPLETE_PAGINATION, f"sync run {run.id}: {broken}"))

    intent_ids = set(session.scalars(
        select(MetaPageFetchAttemptRecord.intent_id).where(
            MetaPageFetchAttemptRecord.workspace_id == context.workspace_id,
            MetaPageFetchAttemptRecord.meta_mode == context.meta_mode,
            MetaPageFetchAttemptRecord.sync_run_id == run.id,
        )
    )) | {page.intent_id for page in pages}
    intents = {
        intent.id: intent for intent in session.scalars(
            select(MetaCallIntentRecord).where(
                MetaCallIntentRecord.workspace_id == context.workspace_id,
                MetaCallIntentRecord.meta_mode == context.meta_mode,
                MetaCallIntentRecord.id.in_(intent_ids),
            )
        )
    } if intent_ids else {}
    answered = set(session.scalars(
        select(MetaCallOutcomeRecord.intent_id).where(
            MetaCallOutcomeRecord.workspace_id == context.workspace_id,
            MetaCallOutcomeRecord.meta_mode == context.meta_mode,
            MetaCallOutcomeRecord.intent_id.in_(intent_ids),
        )
    )) if intent_ids else set()
    reconciliation_id = None
    for intent_id in sorted(intent_ids - answered):
        intent = intents.get(intent_id)
        if intent is None or intent.network_dispatched_at is None:
            continue  # never left the process: nothing on Meta's side to reconcile
        reconciliation = session.scalar(
            select(MetaCallReconciliationRecord)
            .where(
                MetaCallReconciliationRecord.workspace_id == context.workspace_id,
                MetaCallReconciliationRecord.meta_mode == context.meta_mode,
                MetaCallReconciliationRecord.uncertain_intent_id == intent_id,
                MetaCallReconciliationRecord.resolution.in_(_RECONCILED),
            )
            .order_by(MetaCallReconciliationRecord.created_at.desc(), MetaCallReconciliationRecord.id.desc())
            .limit(1)
        )
        if reconciliation is None:
            return replace(base, issue=_blocked(
                RECONCILIATION_UNRESOLVED, f"sync run {run.id}: call intent {intent_id} has no outcome",
            ))
        reconciliation_id = reconciliation.id

    attestation = session.scalar(
        select(EvidenceAttestationRecord)
        .join(EvidencePublicationRecord, EvidencePublicationRecord.id == EvidenceAttestationRecord.publication_id)
        .where(
            EvidencePublicationRecord.workspace_id == context.workspace_id,
            EvidencePublicationRecord.meta_mode == context.meta_mode,
            EvidencePublicationRecord.sync_run_id == run.id,
            EvidencePublicationRecord.state == "published",
            EvidenceAttestationRecord.state == "valid",
        )
        .order_by(EvidenceAttestationRecord.created_at, EvidenceAttestationRecord.id)
        .limit(1)
    )
    if attestation is None:
        return replace(base, issue=_blocked(
            CONTRACT_INSTANCE_FAILED, f"sync run {run.id} has no valid attestation",
        ))

    bodies, edge_pages = [], []
    for page in pages:
        body = json.loads(gzip.decompress(page.payload_compressed))
        bodies.append(body)
        intent = intents.get(page.intent_id)
        edge = (intent.request_parameters or {}).get("edge") if intent is not None else None
        if isinstance(edge, str):
            edge_pages.append((edge, body))
    terminal = pages[-1]
    return replace(
        base,
        bodies=tuple(bodies),
        edge_pages=tuple(edge_pages),
        evidence_hash=canonical_hash(
            [[page.page_order, page.payload_checksum] for page in pages], domain=RUN_EVIDENCE_HASH_DOMAIN,
        ),
        terminal_intent_id=terminal.intent_id,
        terminal_page_id=terminal.id,
        reconciliation_id=reconciliation_id,
        attestation_id=attestation.id,
    )


def _page_chain_break(run: MetaObservationSyncRunRecord, pages: list[MetaEvidencePageRecord]) -> str | None:
    if not pages:
        return "no page"
    if [page.page_order for page in pages] != list(range(1, len(pages) + 1)):
        return "page orders are not 1..n"
    if run.total_pages != len(pages) or (run.last_completed_page_order or 0) != len(pages):
        return f"{len(pages)} pages stored, the run counts {run.total_pages}"
    if not pages[-1].is_terminal or any(page.is_terminal for page in pages[:-1]):
        return "the last page is not the one terminal page"
    if pages[0].input_cursor is not None:
        return "the first page does not start the chain"
    for previous, page in zip(pages, pages[1:]):
        if page.input_cursor != previous.output_cursor:
            return f"page {page.page_order} does not continue page {previous.page_order}"
    for page in pages:
        if hashlib.sha256(page.payload_compressed).hexdigest() != page.payload_checksum:
            return f"page {page.page_order} does not match its checksum"
    return None


def _parse_instance(
    found: _Assessment,
    episode: ScaleEpisodeRecord,
    contract_type: str,
    instance,
    target_date: date,
    binding: AccountBinding,
    evidence: _RunEvidence,
) -> None:
    ad_id = str(episode.ad_remote_id or "")
    if contract_type == DAILY:
        since, until = instance.time_range(target_date)
        observed = parse_daily(evidence.bodies, expected=binding, ad_id=ad_id, since=since, until=until)
        # The read covers the connection's 14 days; the Episode owns only its
        # own (S2 deviation 1).
        days = tuple(day for day in observed.days if day.local_day >= episode.started_local_day)
        found.daily = (evidence, contract_type, days)
    elif contract_type == EXACT_WINDOW:
        since, until = instance.time_range(target_date)
        observed = parse_window(evidence.bodies, expected=binding, ad_id=ad_id, window_role=instance.window_role,
                                since=since, until=until)
        found.windows[instance.window_role] = (evidence, contract_type, observed)
    elif contract_type == DELIVERY_CONFIGURATION and instance.read == CONFIGURATION_READ:
        observed = parse_delivery_configuration(evidence.edge_pages, expected=binding, ad_id=ad_id)
        mismatch = [
            f"{name} {actual!r}, Episode {wanted!r}"
            for name, actual, wanted in (
                ("campaign", observed.campaign_id, episode.campaign_remote_id),
                ("ad set", observed.adset_id, episode.ad_set_remote_id),
            )
            if wanted is not None and actual != wanted
        ]
        if mismatch:
            raise ScaleReadParseError("ad_not_found", "the ad resolves to " + "; ".join(mismatch))
        found.configuration = (evidence, contract_type, observed)
    else:
        raise ScaleReadParseError("malformed_row", f"{contract_type} is not collected in the first batch")


def _status_history(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    generation_id: str,
    target_date: date,
    binding: AccountBinding,
    current: DeliveryConfigurationObservation,
    cache: dict[str, _RunEvidence],
) -> list[dict[str, str]]:
    """The ad's campaign, ad set and ad effective status on every Episode
    day of the lookback whose configuration read succeeded and parses.

    D's read is the manifest's own; an earlier day without a usable read
    leaves a gap the evaluator cannot use to explain a pause, never an
    invented status.
    """
    observations: dict[date, DeliveryConfigurationObservation] = {target_date: current}
    for day in _episode_days(episode, target_date)[:-1]:
        run = session.scalar(
            select(MetaObservationSyncRunRecord)
            .where(
                MetaObservationSyncRunRecord.workspace_id == context.workspace_id,
                MetaObservationSyncRunRecord.meta_mode == context.meta_mode,
                MetaObservationSyncRunRecord.contract_generation_id == generation_id,
                MetaObservationSyncRunRecord.occurrence_id == occurrence_id(generation_id, day, "configuration"),
                MetaObservationSyncRunRecord.state == "succeeded",
            )
            .order_by(MetaObservationSyncRunRecord.run_generation.desc())
            .limit(1)
        )
        if run is None:
            continue
        evidence = _run_evidence(session, context, run, cache)
        if evidence.issue is not None:
            continue
        try:
            observations[day] = parse_delivery_configuration(
                evidence.edge_pages, expected=binding, ad_id=str(episode.ad_remote_id or ""),
            )
        except ScaleReadParseError:
            continue
    return sorted(
        (
            {"local_day": day.isoformat(), "entity_level": level, "status": status}
            for day, observed in observations.items()
            for level, status in (
                ("campaign", observed.campaign_effective_status),
                ("ad_set", observed.adset_effective_status),
                ("ad", observed.ad_effective_status),
            )
        ),
        key=lambda entry: (entry["local_day"], entry["entity_level"]),
    )


def _freshness_issues(found: _Assessment) -> list[_Issue]:
    """Contract §8 at collection: an instance whose run finished more than
    the SLO before the batch's last run is stale evidence."""
    finished = [run.completed_at for run in found.runs if run.completed_at is not None]
    if not finished:
        return []
    slo = timedelta(hours=config.META_FRESHNESS_SLO_HOURS)
    last = max(finished)
    issues = []
    for kind, role, evidence in _used_instances(found):
        if evidence.completed_at is not None and last - evidence.completed_at > slo:
            issues.append(_Issue(
                FRESHNESS_SLO_EXCEEDED, True, kind, role, None,
                f"sync run {evidence.run_id} finished {last - evidence.completed_at} before the batch's last run",
            ))
    return issues


def _used_instances(found: _Assessment):
    if found.daily is not None:
        yield found.daily[1], None, found.daily[0]
    for role, (evidence, kind, _observed) in sorted(found.windows.items()):
        yield kind, role, evidence
    if found.configuration is not None:
        yield found.configuration[1], None, found.configuration[0]


# ---------------------------------------------------------------------------
# Writing the manifest
# ---------------------------------------------------------------------------


def _next_revision(session: Session, context: TenantContext, episode_id: str) -> int:
    """One more than any source revision the Episode has: its manifests and
    its source records (activation's baseline configuration is revision 1
    without a manifest)."""
    highest = 0
    for model in (
        ScaleEvidenceSourceManifestRecord, ScaleDailyMetricSourceRecord, ScaleExactWindowSourceRecord,
        ScaleDeliveryConfigSourceRecord, ScaleBreakdownSourceRecord,
    ):
        value = session.scalar(
            select(func.max(model.source_revision)).where(
                model.workspace_id == context.workspace_id,
                model.meta_mode == context.meta_mode,
                model.episode_id == episode_id,
            )
        )
        highest = max(highest, int(value or 0))
    return highest + 1


def _requested_items(found: _Assessment, episode: ScaleEpisodeRecord, target_date: date) -> list[tuple]:
    """Every requested slot, as ``(contract_type, window_role, local_day)``."""
    slots = []
    for kind in found.requested_types:
        if kind == DAILY:
            slots.extend((kind, None, day) for day in _episode_days(episode, target_date))
            continue
        try:
            contract = contract_for(kind)
        except ScaleReadContractError:
            slots.append((kind, None, None))
            continue
        roles = sorted({instance.window_role for instance in contract.instances}, key=lambda role: role or "")
        slots.extend((kind, role, None) for role in roles)
    return slots


def _minor_units(currency: str) -> Decimal:
    return Decimal(1) if currency in _ZERO_DECIMAL_CURRENCIES else Decimal(100)


def _source_common(context: TenantContext, episode: ScaleEpisodeRecord, revision: int,
                   evidence: _RunEvidence, generation_id: str) -> dict[str, Any]:
    return {
        "workspace_id": context.workspace_id,
        "meta_mode": context.meta_mode,
        "meta_connection_id": episode.meta_connection_id,
        "scenario_scope_key": episode.scenario_scope_key,
        "ad_account_id": episode.ad_account_id,
        "episode_id": episode.id,
        "source_revision": revision,
        "contract_generation_id": generation_id,
        "raw_response_hash": evidence.evidence_hash,
        "sync_run_id": evidence.run_id,
        "occurrence_id": evidence.occurrence_id,
        "call_intent_id": evidence.terminal_intent_id,
        "reconciliation_id": evidence.reconciliation_id,
        "observed_at": evidence.completed_at,
    }


def _generation_of(evidence: _RunEvidence) -> str:
    return evidence.occurrence_id.split(":")[1]


def _write(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    found: _Assessment,
    target_date: date,
    key: str,
) -> ScaleEvidenceSourceManifestRecord:
    revision = _next_revision(session, context, episode.id)
    issues = list(found.issues)
    complete = not found.blocked
    sources: list[Any] = []
    items: list[dict[str, Any]] = []
    digests: dict[tuple, dict[str, Any]] = {}

    if complete and found.daily is not None:
        evidence, _kind, days = found.daily
        for day in days:
            row = ScaleDailyMetricSourceRecord(
                id=new_id("sdms"),
                **_source_common(context, episode, revision, evidence, _generation_of(evidence)),
                local_day=day.local_day,
                state="present",
                delivered=day.delivered,
                spend=day.spend,
                impressions=day.impressions,
                link_clicks=day.link_clicks,
                purchases=day.purchases,
                purchase_value=day.purchase_value,
                currency=episode.currency,
                timezone_name=episode.timezone_name,
                page_attempt_reference=evidence.terminal_page_id,
            )
            sources.append(row)
            digests[(DAILY, None, day.local_day)] = {
                "source_record_id": row.id, "delivered": day.delivered, "spend": day.spend,
                "impressions": day.impressions, "link_clicks": day.link_clicks, "purchases": day.purchases,
                "purchase_value": day.purchase_value, "raw_response_hash": evidence.evidence_hash,
                "sync_run_id": evidence.run_id,
            }
    if complete:
        for role, (evidence, kind, window) in sorted(found.windows.items()):
            if window.frequency is None:
                # No delivery in the window: Meta returns no row, so there is
                # no frequency to keep -- unavailable, never zero (§6.3).
                issues.append(_Issue(EXACT_WINDOW_VALUE_UNAVAILABLE, True, kind, role, None,
                                     f"no delivery in {role}"))
                continue
            row = ScaleExactWindowSourceRecord(
                id=new_id("sews"),
                **_source_common(context, episode, revision, evidence, _generation_of(evidence)),
                window_role=role,
                window_start_local_day=window.since,
                # Half-open, as the snapshot's windows are.
                window_end_local_day=window.until + timedelta(days=1),
                reach=window.reach,
                frequency=window.frequency,
                spend=window.spend,
                impressions=window.impressions,
                link_clicks=window.link_clicks,
                purchases=window.purchases,
                purchase_value=window.purchase_value,
                currency=episode.currency,
                timezone_name=episode.timezone_name,
            )
            sources.append(row)
            digests[(kind, role, None)] = {
                "source_record_id": row.id, "reach": window.reach, "frequency": window.frequency,
                "spend": window.spend, "impressions": window.impressions, "link_clicks": window.link_clicks,
                "purchases": window.purchases, "purchase_value": window.purchase_value,
                "raw_response_hash": evidence.evidence_hash, "sync_run_id": evidence.run_id,
            }
    if complete and found.configuration is not None:
        evidence, kind, observed = found.configuration
        row = _delivery_row(context, episode, revision, evidence, observed, found.status_history)
        sources.append(row)
        digests[(kind, None, None)] = {
            "source_record_id": row.id, "evidence_hash": row.evidence_hash,
            "comparability_hash": row.comparability_hash, "diagnostic_state": row.diagnostic_state,
            "status_history": row.status_history, "raw_response_hash": evidence.evidence_hash,
            "sync_run_id": evidence.run_id,
        }

    for kind, role, day in _requested_items(found, episode, target_date):
        digest = digests.get((kind, role, day))
        if complete:
            state = "fulfilled" if digest is not None else "missing"
        else:
            state = "failed"
        items.append({"contract_type": kind, "window_role": role, "local_day": day, "state": state,
                      "source": digest})

    previous = _latest_complete(session, context, episode.id)
    if complete:
        last_complete, watermark = target_date, target_date
    else:
        last_complete = None
        # A blocked manifest vouches for nothing new: the watermark stays
        # where the last complete manifest left it, or before the Episode.
        watermark = (previous.safe_watermark if previous is not None
                     else episode.started_local_day - timedelta(days=1))
    finished = [run.completed_at for run in found.runs if run.completed_at is not None]
    terminal_at = max(finished) if finished else _as_utc(utcnow())
    request_set_hash = canonical_hash(
        {"target_local_day": target_date, "episode_id": episode.id,
         "members": sorted(found.request_set, key=lambda member: member["contract_type"])},
        domain=REQUEST_SET_HASH_DOMAIN,
    )
    state = "complete" if complete else "blocked"
    issue_payload = sorted(
        (
            {"code": issue.code, "severity": "blocking" if issue.blocking else "warning",
             "contract_type": issue.contract_type, "window_role": issue.window_role,
             "local_day": issue.local_day, "recovery_action": issue.recovery_action,
             "detail": issue.detail}
            for issue in issues
        ),
        key=lambda entry: json.dumps(entry, sort_keys=True, default=str),
    )
    manifest_id = new_id("sesm")
    manifest_hash = canonical_hash(
        {
            "workspace_id": context.workspace_id,
            "meta_mode": context.meta_mode,
            "meta_connection_id": episode.meta_connection_id,
            "scenario_scope_key": episode.scenario_scope_key,
            "episode_id": episode.id,
            "target_local_day": target_date,
            "source_revision": revision,
            "source_generation": SOURCE_GENERATION,
            "state": state,
            "collection_key": key,
            "contract_request_set_hash": request_set_hash,
            "last_complete_local_day": last_complete,
            "safe_watermark": watermark,
            "terminal_at": terminal_at,
            "attestations": sorted({run.attestation_id for run in found.runs if run.attestation_id}),
            "items": sorted(
                items,
                key=lambda item: (item["contract_type"], item["window_role"] or "",
                                  item["local_day"].isoformat() if item["local_day"] else ""),
            ),
            "issues": issue_payload,
        },
        domain=MANIFEST_HASH_DOMAIN,
    )
    manifest = ScaleEvidenceSourceManifestRecord(
        id=manifest_id,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        meta_connection_id=episode.meta_connection_id,
        scenario_scope_key=episode.scenario_scope_key,
        episode_id=episode.id,
        target_local_day=target_date,
        source_revision=revision,
        source_generation=SOURCE_GENERATION,
        state=state,
        contract_request_set_hash=request_set_hash,
        last_complete_local_day=last_complete,
        safe_watermark=watermark,
        terminal_at=terminal_at,
        canonical_hash=manifest_hash,
        collection_key=key,
    )
    # Flushed in dependency order: the composite foreign keys are not
    # ORM relationships, so the unit of work does not sort them.
    session.add_all(sources)
    session.flush()
    session.add(manifest)
    session.flush()
    session.add_all([
        ScaleManifestItemRecord(
            id=new_id("smi"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            manifest_id=manifest.id,
            contract_type=item["contract_type"],
            window_role=item["window_role"],
            local_day=item["local_day"],
            state=item["state"],
            source_record_id=item["source"]["source_record_id"] if item["source"] else None,
        )
        for item in items
    ])
    session.add_all([
        ScaleSourceQualityIssueRecord(
            id=new_id("ssqi"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            manifest_id=manifest.id,
            code=issue.code,
            severity="blocking" if issue.blocking else "warning",
            is_blocking=issue.blocking,
            contract_type=issue.contract_type,
            window_role=issue.window_role,
            local_day=issue.local_day,
            detail=issue.detail,
            recovery_action=issue.recovery_action,
        )
        for issue in issues
    ])
    session.flush()
    _advance_readiness(session, context, episode, manifest, issues)
    append_event(
        session,
        event_type="scale.source_manifest.recorded",
        resource_type="scale_evidence_source_manifest",
        resource_id=manifest.id,
        details={
            "episode_id": episode.id,
            "target_local_day": target_date.isoformat(),
            "source_revision": revision,
            "state": state,
            "collection_key": key,
            "issue_codes": sorted({issue.code for issue in issues}),
            "sync_run_ids": sorted({run.run_id for run in found.runs}),
        },
    )
    session.flush()
    return manifest


def _delivery_row(
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    revision: int,
    evidence: _RunEvidence,
    observed: DeliveryConfigurationObservation,
    status_history: list[dict[str, str]],
) -> ScaleDeliveryConfigSourceRecord:
    """The configuration source: what breaks comparability (§4.3) hashed
    apart from the full observation, so a budget edit or a pause changes
    only the evidence hash."""
    budget = Decimal(observed.budget_amount_minor) / _minor_units(observed.currency)
    comparability = {
        "ad_creative_id": observed.ad_creative_id,
        "campaign_id": observed.campaign_id,
        "adset_id": observed.adset_id,
        "ad_id": observed.ad_id,
        "optimization_goal": observed.optimization_goal,
        "bid_strategy": observed.bid_strategy,
        "attribution_spec_hash": observed.attribution_spec_hash,
        "targeting_safety_hash": observed.targeting_safety_hash,
        "offer_version_id": episode.offer_version_id,
        "landing_page_version_id": episode.landing_page_version_id,
    }
    known = observed.learning_stage != "unknown"
    full = {
        **comparability,
        "campaign_status": observed.campaign_effective_status,
        "ad_set_status": observed.adset_effective_status,
        "ad_status": observed.ad_effective_status,
        "budget_source": observed.budget_source,
        "budget_amount": budget,
        "currency": observed.currency,
        "learning_stage": observed.learning_stage,
        "raw_response_hash": evidence.evidence_hash,
    }
    return ScaleDeliveryConfigSourceRecord(
        id=new_id("sdcs"),
        **_source_common(context, episode, revision, evidence, _generation_of(evidence)),
        ad_creative_id=observed.ad_creative_id,
        campaign_remote_id=observed.campaign_id,
        ad_set_remote_id=observed.adset_id,
        ad_remote_id=observed.ad_id,
        campaign_status=observed.campaign_effective_status,
        ad_set_status=observed.adset_effective_status,
        ad_status=observed.ad_effective_status,
        budget_source=observed.budget_source,
        budget_amount=budget,
        currency=episode.currency,
        optimization_goal=observed.optimization_goal,
        bid_strategy=observed.bid_strategy,
        attribution_setting=observed.attribution_spec_hash,
        targeting_safety_hash=observed.targeting_safety_hash,
        offer_version_id=episode.offer_version_id,
        landing_page_version_id=episode.landing_page_version_id,
        evidence_hash=canonical_hash(full, domain=DELIVERY_EVIDENCE_HASH_DOMAIN),
        comparability_hash=canonical_hash(comparability, domain=DELIVERY_COMPARABILITY_HASH_DOMAIN),
        diagnostic_state=observed.learning_stage,
        diagnostic_source_field=_DIAGNOSTIC_FIELD if known else None,
        diagnostic_observed_at=evidence.completed_at if known else None,
        status_history=status_history,
    )


def _latest_complete(
    session: Session, context: TenantContext, episode_id: str,
) -> ScaleEvidenceSourceManifestRecord | None:
    return session.scalar(
        select(ScaleEvidenceSourceManifestRecord)
        .where(
            ScaleEvidenceSourceManifestRecord.workspace_id == context.workspace_id,
            ScaleEvidenceSourceManifestRecord.meta_mode == context.meta_mode,
            ScaleEvidenceSourceManifestRecord.episode_id == episode_id,
            ScaleEvidenceSourceManifestRecord.state == "complete",
        )
        .order_by(ScaleEvidenceSourceManifestRecord.source_revision.desc())
        .limit(1)
    )


# ---------------------------------------------------------------------------
# Readiness projection (spec §1.7, contract §8)
# ---------------------------------------------------------------------------


def _recovery_action(issues: list[_Issue]) -> str | None:
    order = list(RECOVERY_ACTIONS)
    ranked = sorted((issue for issue in issues if issue.recovery_action),
                    key=lambda issue: order.index(issue.code))
    return ranked[0].recovery_action if ranked else None


def _advance_readiness(
    session: Session,
    context: TenantContext,
    episode: ScaleEpisodeRecord,
    manifest: ScaleEvidenceSourceManifestRecord,
    issues: list[_Issue],
) -> ScaleEvidenceReadinessProjectionRecord:
    """Move the Episode's readiness to ``manifest``, never back to an older
    source revision.

    A blocked manifest -- or a complete one with a blocking issue -- is
    maturity ``insufficient``, quality ``blocked`` (contract §8); the last
    complete day and freshness stay where the last complete manifest put
    them. Otherwise maturity is what the accepted Evaluation said once it
    reached ``monitorable`` or better, and ``warming`` until then: readiness
    never invents an evaluator verdict.
    """
    projection = session.scalar(
        select(ScaleEvidenceReadinessProjectionRecord)
        .where(
            ScaleEvidenceReadinessProjectionRecord.workspace_id == context.workspace_id,
            ScaleEvidenceReadinessProjectionRecord.meta_mode == context.meta_mode,
            ScaleEvidenceReadinessProjectionRecord.episode_id == episode.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if projection is not None and projection.latest_manifest_id is not None:
        current = session.scalar(
            select(ScaleEvidenceSourceManifestRecord.source_revision).where(
                ScaleEvidenceSourceManifestRecord.workspace_id == context.workspace_id,
                ScaleEvidenceSourceManifestRecord.meta_mode == context.meta_mode,
                ScaleEvidenceSourceManifestRecord.id == projection.latest_manifest_id,
            )
        )
        if current is not None and current >= manifest.source_revision:
            return projection
    blocking = sum(1 for issue in issues if issue.blocking)
    warning = len(issues) - blocking
    if manifest.state == "blocked" or blocking:
        quality, maturity = "blocked", "insufficient"
    else:
        quality = "warning" if warning else "pass"
        evaluated = session.scalar(
            select(ScaleEpisodeProjectionRecord.maturity).where(
                ScaleEpisodeProjectionRecord.workspace_id == context.workspace_id,
                ScaleEpisodeProjectionRecord.meta_mode == context.meta_mode,
                ScaleEpisodeProjectionRecord.episode_id == episode.id,
            )
        )
        maturity = evaluated if evaluated in ("monitorable", "economics_ready") else "warming"
    complete = manifest.state == "complete"
    now = utcnow()
    if projection is None:
        projection = ScaleEvidenceReadinessProjectionRecord(
            id=new_id("serp"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            episode_id=episode.id,
            revision=0,
            created_at=now,
        )
        session.add(projection)
    projection.latest_manifest_id = manifest.id
    projection.maturity = maturity
    projection.quality_state = quality
    if complete:
        projection.last_complete_local_day = manifest.last_complete_local_day
        projection.freshness_reference_at = manifest.terminal_at
    projection.blocking_issue_count = blocking
    projection.warning_issue_count = warning
    projection.recovery_action = _recovery_action(issues)
    projection.revision = (projection.revision or 0) + 1
    projection.updated_at = now
    session.flush()
    return projection
