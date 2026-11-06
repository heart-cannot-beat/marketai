"""Localization Worker module (Issue #24, spec §2G/§2H, §16.8/§16.10).

Job handlers that drive one ``LocalizationItem`` through the provider
Attempt lifecycle: edit dispatch -> deterministic QC -> (frozen) external
QC -> finalize, plus the two source-level preparation calls
(``localization.analyze_source`` / ``localization.translate_copy``) and the
batch-level fold. Follows ``app.generation.worker`` structurally --
state guards before every transition, CAS ``UPDATE ... WHERE revision =
expected`` on every ``LocalizationItemRecord`` mutation (``StaleRevisionError``
from ``app.localization.batch``, reused rather than redefined), and durable
Job enqueuing via ``app.platform.jobs.enqueue_in_session`` for the next
pipeline stage.

Every external/paid step (``analyze_source``, ``translate_copy``,
``edit_attempt``) dispatches through ``app.providers.dispatch
.ExternalCallCoordinator``'s three-phase prepare/dispatch/complete cycle
(spec §2D) -- this module is the coordinator's first real caller (Task 10's
own report: "no real caller yet ... that's Task 16's
app/localization/worker.py"). Because the coordinator's ``execute()`` is
``async`` (phase 2 awaits the adapter's network call outside any DB
transaction), every handler that reaches it is `async def`; the "synchronous
handlers" structural pattern from ``app.generation.worker`` is followed in
every other respect (state guards, CAS, one session per transactional
phase) but cannot itself be synchronous without bypassing the coordinator
the spec requires this module to use.

Only ``provider_execution_mode == "mock"`` is dispatched (Controlled Mock,
CLAUDE.md invariant: "Controlled Mock is explicit and cannot mix with real
evidence"); any other mode raises ``NotImplementedError``, mirroring
``app.generation.worker._call_adapter``'s Task-3-scope ruling exactly.

OCR and visual QC are, per this task's brief, "placeholder/mock for v1":
rather than building a second full coordinator/evidence-recorder pipeline
for two checks with no real provider yet, ``handle_ocr_check`` and
``handle_visual_qc`` are local, synchronous, always-pass placeholders that
append their own ``LocalizationQcRecordRecord`` directly -- no
``ProviderCallAttempt``, no Job dispatch through the coordinator. If OCR or
visual QC later gains a real external call, spec §2G is explicit that it
"remains its own Job and reservation" -- these two handlers are the seam a
later task upgrades, not a shortcut this task takes on the coordinator's
behalf.

Preparation-phase evidence (``localization.analyze_source`` /
``localization.translate_copy``) and item-phase evidence
(``localization.edit_attempt``) each get their own
``ExternalCallEvidenceRecorder`` that creates the "work row"
(``LocalizationPreparationProviderCallRecord`` /
``LocalizationItemProviderCallRecord``) *at prepare time* rather than
linking a pre-existing one -- ``app.localization.batch`` (Task 14) enqueues
the Job but never creates that work row itself, so this module's
``link_prepared_attempt`` is where it first comes into existence, one row
per ``ProviderCallAttempt`` (spec §1.9's composite FKs are satisfied by
construction: every column mirrors the just-created attempt's own).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Callable, ContextManager, Mapping, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    AssetVersionRecord,
    JobRecord,
    LocalizationAttemptOutputRecord,
    LocalizationAttemptRecord,
    LocalizationBatchCommandRecord,
    LocalizationBatchRecord,
    LocalizationBatchSourceRecord,
    LocalizationItemProviderCallRecord,
    LocalizationItemRecord,
    LocalizationOutputPublicationRecord,
    LocalizationPreparationProviderCallRecord,
    LocalizationQcRecordRecord,
    LocalizedAssetLineageRecord,
    LocalizedCopyVersionRecord,
    new_id,
    utcnow,
)
from app.entitlements.cost_models import ProviderCallAttemptRecord
from app.localization import batch as batch_mod
from app.localization import binding as binding_mod
from app.localization import claims as claims_mod
from app.localization import copy as copy_mod
from app.localization import publication as publication_mod
from app.localization import qc as qc_mod
from app.localization.copy import prepare_copy_segments
from app.localization.eligibility import validate_source_for_batch
from app.localization.truth import assess_truth_freshness
from app.platform.job_outcomes import (
    HandlerOutcomeSignal,
    JobFailed,
    JobObsolete,
    JobUncertain,
)
from app.platform.jobs import enqueue_in_session
from app.platform.operations import aggregate_operation_from_jobs
from app.platform.tenancy import TenantContext
from app.providers.adapter import ProviderCapabilities, ProviderResult
from app.providers.dispatch import ExternalCallCoordinator

logger = logging.getLogger(__name__)

SessionFactory = Callable[[TenantContext], ContextManager[Session]]

# ``localization_batches.provider_execution_mode`` -> the experiment
# evidence-mode vocabulary ``validate_source_for_batch`` expects (spec
# §3C) -- the same normalization ``app.localization.batch`` and
# ``app.localization.eligibility`` each keep their own copy of.
_PROVIDER_TO_EXPERIMENT_MODE = {"byok": "production", "sandbox": "sandbox", "mock": "mock"}

_TERMINAL_BATCH_PHASES = ("cancelled", "closed")
_TERMINAL_ITEM_STATES = ("succeeded", "failed", "skipped", "cancelled")

#: LocalizationItem states from which the edit handler is willing to start
#: (or resume, on a redelivered Job) dispatch (spec §2G point 1: "The edit
#: handler accepts only an authorized queued Attempt"). Anything else means
#: another attempt is already in flight, the item is already past this
#: stage, or it is paused pending an explicit resume/retry -- re-entering
#: `handle_edit_attempt` on it is a silent no-op, not a CAS error, so a
#: redelivered Job never corrupts an already-advanced Item.
#:
#: `running` is included alongside `queued`: a crash between the first
#: session's `queued -> running` commit and the second session's
#: `coordinator.execute()` call would otherwise permanently strand the
#: Item (nothing else would ever re-enter it). Re-entering with `running`
#: is safe -- the coordinator's own `job_id`-keyed idempotent replay
#: (`ExternalCallCoordinator`'s crash windows 1/2/4) resumes/replays the
#: exact same Attempt rather than dispatching a second one.
_EDIT_DISPATCHABLE_STATES = ("queued", "running")


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _requested_evidence_tier(batch: LocalizationBatchRecord) -> str:
    return _PROVIDER_TO_EXPERIMENT_MODE.get(batch.provider_execution_mode, "mock")


def _lock_preparation_batch(
    session: Session, ctx: TenantContext, batch_id: str
) -> LocalizationBatchRecord | None:
    """Serialize preparation completions before deriving their projection."""

    return session.scalar(
        select(LocalizationBatchRecord)
        .where(
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.id == batch_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


# ---------------------------------------------------------------------------
# Typed request/result value objects (spec §2D: Localization owns these)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalyzeSourceRequest:
    asset_version_id: str
    analysis_recipe_hash: str


@dataclass(frozen=True)
class AnalyzeSourceResult:
    claims: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class TranslateCopyRequest:
    segments: tuple[tuple[int, str], ...]  # (claim_index, source text)
    target_locale: str


@dataclass(frozen=True)
class TranslateCopyResult:
    translated_segments: Mapping[int, str]


@dataclass(frozen=True)
class EditAttemptRequest:
    item_id: str
    attempt_number: int
    source_object_key: str
    copy_content_hash: str


@dataclass(frozen=True)
class EditAttemptResult:
    object_key: str
    content_sha256: str
    byte_size: int
    mime_type: str


# ---------------------------------------------------------------------------
# Deterministic mock adapters (Controlled Mock only -- CLAUDE.md invariant)
# ---------------------------------------------------------------------------


class _DeterministicMockAdapter:
    """A minimal ``ExternalCallAdapter`` whose output is a pure function of
    its typed request -- the same "no randomness" ruling
    ``app.generation.worker._call_adapter`` documents: a redelivered Job or
    a reconciliation replay must reproduce byte-identical mock output."""

    provider = "controlled_mock"
    capabilities = ProviderCapabilities(
        accepts_idempotency_key=True, supports_result_lookup_by_request_id=False,
    )

    def __init__(self, fn):
        self._fn = fn

    async def execute(self, request, *, credential, idempotency_key):
        output = self._fn(request)
        return ProviderResult(
            output=output,
            provider_request_id=f"mockreq_{idempotency_key}",
            idempotency_key_accepted=True,
        )


def _mock_analyze(request: AnalyzeSourceRequest) -> AnalyzeSourceResult:
    # Deterministic Controlled Mock finds no new Claims -- a real adapter
    # (out of this task's scope) is what actually observes source text.
    del request
    return AnalyzeSourceResult(claims=())


def _mock_translate(request: TranslateCopyRequest) -> TranslateCopyResult:
    translated = {
        claim_index: f"[{request.target_locale}] {text}"
        for claim_index, text in request.segments
    }
    return TranslateCopyResult(translated_segments=translated)


def _mock_edit(request: EditAttemptRequest) -> EditAttemptResult:
    content = f"loc_edit_{request.item_id}_{request.attempt_number}_{request.copy_content_hash[:16]}"
    return EditAttemptResult(
        object_key=f"mock/localized/{content}.png",
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        byte_size=1024,
        mime_type="image/png",
    )


_DEFAULT_ADAPTER_REGISTRY: dict[str, _DeterministicMockAdapter] = {
    "analyze_source": _DeterministicMockAdapter(_mock_analyze),
    "translate_copy": _DeterministicMockAdapter(_mock_translate),
    "edit_attempt": _DeterministicMockAdapter(_mock_edit),
}


# ---------------------------------------------------------------------------
# Evidence recorders (spec §2D seam)
# ---------------------------------------------------------------------------


class _PreparationCallEvidenceRecorder:
    """Evidence recorder for ``analyze_source``/``translate_copy`` -- creates
    (and links) the ``LocalizationPreparationProviderCallRecord`` work row
    at prepare time, then folds a successful response into the real
    Inventory/CopyVersion in the same completion transaction."""

    def __init__(
        self,
        *,
        work_kind: str,  # "analysis" | "translation"
        batch: LocalizationBatchRecord,
        batch_source: LocalizationBatchSourceRecord,
        batch_command_id: str,
        analysis_recipe_hash: str | None = None,
        input_inventory_id: str | None = None,
        truth_package_market_id: str | None = None,
        target_market: str | None = None,
        market_policy_version_id: str | None = None,
        claim_decision_set_id: str | None = None,
        copy_kwargs: Mapping[str, object] | None = None,
    ) -> None:
        self.work_kind = work_kind
        self.batch = batch
        self.batch_source = batch_source
        self.batch_command_id = batch_command_id
        self.analysis_recipe_hash = analysis_recipe_hash
        self.input_inventory_id = input_inventory_id
        self.truth_package_market_id = truth_package_market_id
        self.target_market = target_market
        self.market_policy_version_id = market_policy_version_id
        self.claim_decision_set_id = claim_decision_set_id
        self.copy_kwargs = dict(copy_kwargs or {})

    def link_prepared_attempt(self, session: Session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord) -> None:
        session.add(
            LocalizationPreparationProviderCallRecord(
                id=new_id("lppc"),
                workspace_id=ctx.workspace_id,
                batch_id=self.batch.id,
                batch_source_id=self.batch_source.id,
                source_asset_version_id=self.batch_source.asset_version_id,
                batch_command_id=self.batch_command_id,
                operation_id=attempt.operation_id,
                truth_package_id=attempt.truth_package_id,
                provider_execution_mode=attempt.execution_mode,
                work_kind=self.work_kind,
                provider_step_kind=attempt.step_kind,
                input_inventory_id=self.input_inventory_id,
                truth_package_market_id=self.truth_package_market_id,
                target_market=self.target_market,
                market_policy_version_id=self.market_policy_version_id,
                claim_decision_set_id=self.claim_decision_set_id,
                subject_semantic_key_hash=attempt.call_semantic_key_hash,
                provider_call_attempt_id=attempt.id,
                result_inventory_id=None,
                result_copy_version_id=None,
                state="prepared",
                revision=1,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        session.flush()

    def materialize(self, ctx: TenantContext, *, attempt_id: str, output) -> str:
        del ctx
        return f"ref:{attempt_id}"

    def persist_result(self, session: Session, ctx: TenantContext, *, attempt, reference, output) -> None:
        work = session.scalar(
            select(LocalizationPreparationProviderCallRecord).where(
                LocalizationPreparationProviderCallRecord.workspace_id == ctx.workspace_id,
                LocalizationPreparationProviderCallRecord.provider_call_attempt_id == attempt.id,
            )
        )
        now = utcnow()
        if self.work_kind == "analysis":
            inventory = claims_mod.create_inventory_from_analysis(
                session, ctx,
                asset_version_id=self.batch_source.asset_version_id,
                analysis_recipe_hash=self.analysis_recipe_hash,
                claims=list(output.claims),
                provider=attempt.provider, model=attempt.model or "",
                rule_version="claims-rule-v1",
                analysis_execution_mode=attempt.execution_mode,
                adapter_version="mock-v1",
                mock_scenario="default" if attempt.execution_mode == "mock" else None,
                mock_version="v1" if attempt.execution_mode == "mock" else None,
                now=now,
            )
            if work is not None:
                work.result_inventory_id = inventory.id
                work.state = "succeeded"
                work.completed_at = now
                work.updated_at = now
                session.flush()
                batch = _lock_preparation_batch(
                    session, ctx, self.batch.id
                )
                source_count = len(
                    session.scalars(
                        select(LocalizationBatchSourceRecord).where(
                            LocalizationBatchSourceRecord.workspace_id
                            == ctx.workspace_id,
                            LocalizationBatchSourceRecord.batch_id == self.batch.id,
                        )
                    ).all()
                )
                if (
                    batch is not None
                    and batch.preparation_stage == "analysis"
                    and len(
                        batch_mod._bound_inventory_ids(
                            session, ctx, batch
                        )
                    )
                    == source_count
                ):
                    coverage_findings = (
                        batch_mod._frozen_policy_coverage_findings(
                            session, ctx, batch
                        )
                    )
                    recipe = dict(batch.frozen_recipe_json or {})
                    recipe["analysis_validation_findings"] = list(
                        coverage_findings
                    )
                    batch.frozen_recipe_json = recipe
                    batch.preparation_stage = (
                        "truth" if coverage_findings else "claim_review"
                    )
                    batch.revision += 1
                    batch.updated_at = now
        else:
            created = copy_mod.create_copy_from_translation(
                session, ctx,
                translated_segments=dict(output.translated_segments),
                translation_execution_mode=attempt.execution_mode,
                translation_provider=attempt.provider,
                translation_model=attempt.model or "",
                translation_adapter_version="mock-v1",
                mock_scenario="default" if attempt.execution_mode == "mock" else None,
                mock_version="v1" if attempt.execution_mode == "mock" else None,
                now=now,
                **self.copy_kwargs,
            )
            if work is not None:
                work.result_copy_version_id = created.copy_version.id
                work.state = "succeeded"
                work.completed_at = now
                work.updated_at = now
                session.flush()
                batch = _lock_preparation_batch(
                    session, ctx, self.batch.id
                )
                if batch is not None:
                    batch_mod._refresh_preparation_projection(
                        session,
                        ctx,
                        batch,
                        bump_revision_on_change=True,
                        now=now,
                    )
        session.flush()


class _EditCallEvidenceRecorder:
    """Evidence recorder for ``localization.edit_attempt`` -- links the
    already-existing (authorized) ``LocalizationAttemptRecord`` and, on
    success, persists the immutable isolated ``LocalizationAttemptOutputRecord``
    (spec §2G point 3: "Persist a successful response as an immutable
    isolated AttemptOutput before doing anything publishable")."""

    def __init__(self, *, item: LocalizationItemRecord, attempt: LocalizationAttemptRecord, batch_command_id: str) -> None:
        self.item = item
        self.attempt = attempt
        self.batch_command_id = batch_command_id

    def link_prepared_attempt(self, session: Session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord) -> None:
        session.add(
            LocalizationItemProviderCallRecord(
                id=new_id("lipc"),
                workspace_id=ctx.workspace_id,
                item_id=self.item.id,
                attempt_id=self.attempt.id,
                batch_id=self.item.batch_id,
                batch_command_id=self.batch_command_id,
                operation_id=attempt.operation_id,
                truth_package_id=attempt.truth_package_id,
                provider_execution_mode=attempt.execution_mode,
                provider_step_kind=attempt.step_kind,
                provider_call_attempt_id=attempt.id,
            )
        )
        session.flush()

    def materialize(self, ctx: TenantContext, *, attempt_id: str, output) -> str:
        del ctx
        return f"ref:{attempt_id}"

    def persist_result(self, session: Session, ctx: TenantContext, *, attempt, reference, output) -> None:
        del reference
        evidence_hash = _hash(
            {
                "object_key": output.object_key,
                "content_sha256": output.content_sha256,
                "attempt_id": self.attempt.id,
            }
        )
        session.add(
            LocalizationAttemptOutputRecord(
                id=new_id("lao"),
                workspace_id=ctx.workspace_id,
                attempt_id=self.attempt.id,
                item_id=self.item.id,
                batch_id=self.item.batch_id,
                object_key=output.object_key,
                content_sha256=output.content_sha256,
                byte_size=output.byte_size,
                mime_type=output.mime_type,
                output_evidence_hash=evidence_hash,
                created_at=utcnow(),
            )
        )
        session.flush()


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_batch(session: Session, ctx: TenantContext, batch_id: str) -> LocalizationBatchRecord:
    return session.scalar(
        select(LocalizationBatchRecord).where(
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.id == batch_id,
        )
    )


def _load_item(session: Session, ctx: TenantContext, item_id: str, batch_id: str) -> LocalizationItemRecord | None:
    return session.scalar(
        select(LocalizationItemRecord).where(
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.id == item_id,
            LocalizationItemRecord.batch_id == batch_id,
        )
    )


def _load_attempt_by_number(
    session: Session, ctx: TenantContext, item_id: str, batch_id: str, attempt_number: int,
) -> LocalizationAttemptRecord | None:
    return session.scalar(
        select(LocalizationAttemptRecord).where(
            LocalizationAttemptRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptRecord.item_id == item_id,
            LocalizationAttemptRecord.batch_id == batch_id,
            LocalizationAttemptRecord.attempt_number == attempt_number,
        )
    )


def _load_attempt(session: Session, ctx: TenantContext, attempt_id: str) -> LocalizationAttemptRecord | None:
    return session.scalar(
        select(LocalizationAttemptRecord).where(
            LocalizationAttemptRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptRecord.id == attempt_id,
        )
    )


def _get_attempt_output(session: Session, ctx: TenantContext, attempt_id: str) -> LocalizationAttemptOutputRecord | None:
    return session.scalar(
        select(LocalizationAttemptOutputRecord).where(
            LocalizationAttemptOutputRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptOutputRecord.attempt_id == attempt_id,
        )
    )


def _find_job(
    session: Session, ctx: TenantContext, job_type: str, resource_type: str, resource_id: str,
    *, payload_filter: Mapping[str, object] | None = None,
) -> JobRecord | None:
    rows = session.scalars(
        select(JobRecord).where(
            JobRecord.workspace_id == ctx.workspace_id,
            JobRecord.type == job_type,
            JobRecord.resource_type == resource_type,
            JobRecord.resource_id == resource_id,
        )
    ).all()
    if not payload_filter:
        return rows[0] if rows else None
    for row in rows:
        payload = row.payload_json or {}
        if all(payload.get(k) == v for k, v in payload_filter.items()):
            return row
    return None


def _find_batch_command_for_operation(
    session: Session, ctx: TenantContext, operation_id: str,
) -> LocalizationBatchCommandRecord | None:
    return session.scalar(
        select(LocalizationBatchCommandRecord).where(
            LocalizationBatchCommandRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchCommandRecord.operation_id == operation_id,
        )
    )


def _call_semantic_key(job: JobRecord) -> str:
    """Recover the bounded call hash persisted inside an admitted Job key.

    Admission namespaces ``Job.idempotency_key`` as
    ``{operation_id}_{call_semantic_key_hash}``, while ProviderCallAttempt's
    identity column stores the original 64-character call hash.  Passing the
    namespaced Job key leaks an implementation prefix into provider identity
    and exceeds the PostgreSQL column bound.
    """
    value = job.idempotency_key
    if job.operation_id is not None:
        prefix = f"{job.operation_id}_"
        if value.startswith(prefix):
            value = value[len(prefix):]
    if not value or len(value) > 64:
        raise ValueError(
            f"Job {job.id!r} has no bounded call semantic key"
        )
    return value


# ---------------------------------------------------------------------------
# LocalizationItem CAS transitions (spec §3F: "all Item transitions use CAS")
# ---------------------------------------------------------------------------


def _transition_item(
    session: Session,
    ctx: TenantContext,
    item: LocalizationItemRecord,
    *,
    state: str,
    reason_code: str | None = None,
    reason_detail: str | None = None,
    recoverable: bool | None = None,
    next_action: str | None = None,
    clear_reason: bool = False,
) -> LocalizationItemRecord:
    values: dict = {"state": state, "revision": item.revision + 1, "updated_at": utcnow()}
    if clear_reason:
        values.update(reason_code=None, reason_detail=None, recoverable=None, next_action=None)
    else:
        if reason_code is not None:
            values["reason_code"] = reason_code
        if reason_detail is not None:
            values["reason_detail"] = reason_detail[:500]
        if recoverable is not None:
            values["recoverable"] = recoverable
        if next_action is not None:
            values["next_action"] = next_action

    result = session.execute(
        update(LocalizationItemRecord)
        .where(
            LocalizationItemRecord.id == item.id,
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.revision == item.revision,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise batch_mod.StaleRevisionError("item", item.id, item.revision)
    session.flush()
    session.refresh(item)
    return item


def _cancel_unstarted_jobs(session: Session, ctx: TenantContext, *, resource_type: str, resource_id: str) -> int:
    """Same effect as ``app.platform.jobs.cancel_resource``, but runs on the
    caller's already-open ``session``/transaction instead of opening its own
    nested ``tenant_session`` -- calling ``cancel_resource`` from inside an
    already-open handler session deadlocks the (single-writer) SQLite
    connection.

    Covers both Job-creation origins this module's pipeline can hit:
    ``PaidOperationAdmission.admit_in_transaction`` (``app.localization
    .batch``'s ``confirm()``/``retry_item()``, which pre-admit the frozen
    external QC steps' Jobs) creates rows starting ``pending``, while
    ``enqueue_in_session`` (this module's own QC/finalize/fold enqueues)
    starts them ``queued`` -- both are "not yet started" and must be
    cancellable here.
    """
    cancellable = ("pending", "queued", "retry_wait", "leased")
    operation_ids = {
        job.operation_id
        for job in session.scalars(
            select(JobRecord).where(
                JobRecord.resource_type == resource_type,
                JobRecord.resource_id == resource_id,
                JobRecord.workspace_id == ctx.workspace_id,
                JobRecord.state.in_(cancellable),
            )
        )
        if job.operation_id
    }
    result = session.execute(
        update(JobRecord)
        .where(
            JobRecord.resource_type == resource_type,
            JobRecord.resource_id == resource_id,
            JobRecord.workspace_id == ctx.workspace_id,
            JobRecord.state.in_(cancellable),
        )
        .values(state="cancelled", finished_at=utcnow(), lease_owner=None, lease_expires_at=None)
    )
    session.flush()
    # Cancelling here can make a command's last outstanding Job terminal, so
    # the Operation has to be folded exactly as `jobs.cancel_resource` folds
    # it. Without this the paid Operation behind a deterministic-QC failure
    # sits `running` with every sibling Job finished, until the Worker's idle
    # reconciliation sweep happens to notice.
    for operation_id in sorted(operation_ids):
        aggregate_operation_from_jobs(
            session,
            operation_id,
            actor=ctx.executor or "system:worker",
            context=ctx,
            reason="sibling Job cancelled with its Localization resource",
        )
    return int(result.rowcount or 0)


def _check_source_truth_copy_eligibility(
    session: Session, ctx: TenantContext, item: LocalizationItemRecord, batch: LocalizationBatchRecord,
) -> tuple[bool, list[str]]:
    """The recheck spec §4.2/§2G/§16.10 require before every external/paid
    step and before publish: current source eligibility, Truth freshness,
    and Copy approval. Shared by the pre-dispatch check, the post-dispatch
    check, the resume-after-pause re-entry below, and ``handle_finalize_item``."""
    report = validate_source_for_batch(
        session, ctx, item.source_asset_version_id, item.decision_id,
        requested_evidence_tier=_requested_evidence_tier(batch),
    )
    freshness = assess_truth_freshness(session, ctx, batch.truth_package_id, at=utcnow())
    copy_version = session.get(LocalizedCopyVersionRecord, item.copy_version_id)
    copy_ok = copy_version is not None and copy_version.state == "approved"
    findings = list(report.findings) + list(freshness.reasons)
    if not copy_ok:
        findings.append("copy_not_approved")
    return (report.valid and freshness.is_current and copy_ok), findings


def _pause_item(session: Session, ctx: TenantContext, item: LocalizationItemRecord, *, reason_code: str, findings: Sequence[str]) -> None:
    _transition_item(
        session, ctx, item, state="paused",
        reason_code=reason_code, reason_detail=";".join(findings) or None,
        recoverable=True, next_action="resume_item",
    )


def _handle_attempt_failure(
    session: Session,
    ctx: TenantContext,
    item: LocalizationItemRecord,
    attempt: LocalizationAttemptRecord,
    batch: LocalizationBatchRecord,
    *,
    reason_code: str,
    reason_detail: str = "",
) -> None:
    """Spec §2G point 8 / §16.8: bounded retry. On failure before the
    Batch's frozen ``max_attempts``, move to ``retry_wait`` -- no further
    paid call is scheduled until an explicit ``retry_item`` command. On
    failure at the last permitted Attempt, move to terminal
    ``failed``/``qc_exhausted``."""
    # A provider-completed Attempt is terminal even when later QC rejects
    # its isolated output.  Only an in-flight provider Attempt itself moves
    # to failed; QC failure is represented by the Item state/reason and the
    # immutable QC records.
    if attempt.state in ("authorized", "dispatching"):
        attempt.state = "failed"
        session.flush()

    if attempt.attempt_number < batch.max_attempts:
        _transition_item(
            session, ctx, item, state="retry_wait",
            reason_code=reason_code, reason_detail=reason_detail or reason_code,
            recoverable=True, next_action="retry_item",
        )
    else:
        _transition_item(
            session, ctx, item, state="failed",
            reason_code="qc_exhausted", reason_detail=reason_detail or reason_code,
            recoverable=False, next_action="create_new_batch",
        )
        enqueue_in_session(
            session,
            context=ctx,
            job_type="localization.fold_batch",
            resource_type="localization_batch",
            resource_id=batch.id,
            payload={"batch_id": batch.id},
            idempotency_key=f"fold:{batch.id}:{item.id}",
        )


# ---------------------------------------------------------------------------
# handle_analyze_source
# ---------------------------------------------------------------------------


def _settle_preparation_call(
    ctx: TenantContext,
    result,
    session_factory: SessionFactory,
    *,
    work_kind: str,
) -> None:
    """Record a preparation call's non-success and signal it to the queue.

    Marking the ``LocalizationPreparationProviderCallRecord`` failed or
    uncertain is only half the story: without a signal the Worker fence sees
    a handler that returned normally, settles the Job ``succeeded``, and the
    Operation aggregates to ``succeeded`` for a batch whose preparation
    never produced anything. The raise is what keeps the Job state, the
    preparation evidence and the Operation telling the same story.
    """
    if result.success or result.replayed:
        return

    uncertain = result.dispatch_certainty == "uncertain"
    with session_factory(ctx) as session:
        work = session.scalar(
            select(LocalizationPreparationProviderCallRecord).where(
                LocalizationPreparationProviderCallRecord.workspace_id == ctx.workspace_id,
                LocalizationPreparationProviderCallRecord.provider_call_attempt_id == result.attempt_id,
            )
        )
        if work is not None and work.state == "prepared":
            work.state = "uncertain" if uncertain else "failed"
            work.updated_at = utcnow()
            session.flush()

    detail = result.error_detail or ""
    if uncertain:
        raise JobUncertain(
            result.error_code or f"{work_kind}_dispatch_uncertain",
            detail or "preparation call left uncertain; requires reconciliation",
        )
    raise JobFailed(
        result.error_code or f"{work_kind}_dispatch_failed",
        detail or f"{work_kind} preparation call failed",
    )


async def handle_analyze_source(
    ctx: TenantContext,
    *,
    batch_id: str,
    asset_version_id: str,
    analysis_recipe_hash: str,
    session_factory: SessionFactory,
    coordinator: ExternalCallCoordinator,
    adapter_registry: Mapping[str, object] | None = None,
) -> None:
    registry = adapter_registry or _DEFAULT_ADAPTER_REGISTRY

    with session_factory(ctx) as session:
        batch = _load_batch(session, ctx, batch_id)
        if batch is None or batch.phase in _TERMINAL_BATCH_PHASES:
            raise JobObsolete(
                "batch_terminal",
                "the Batch reached a terminal phase before this Job was claimed",
            )
        # Idempotent no-op: analysis already landed for this exact recipe.
        plan = claims_mod.plan_inventories(
            session, ctx, asset_version_ids=[asset_version_id], analysis_recipe_hash=analysis_recipe_hash,
        )
        if asset_version_id in plan.reused:
            return

        batch_source = session.scalar(
            select(LocalizationBatchSourceRecord).where(
                LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
                LocalizationBatchSourceRecord.batch_id == batch_id,
                LocalizationBatchSourceRecord.asset_version_id == asset_version_id,
            )
        )
        if batch_source is None:
            return

        report = validate_source_for_batch(
            session, ctx, asset_version_id, batch_source.decision_id,
            requested_evidence_tier=_requested_evidence_tier(batch),
        )
        if not report.valid:
            # No Item exists yet to pause -- nothing spent, nothing to
            # undo. A later `submit_claim_decisions` naturally reports
            # `analysis_incomplete` until analysis succeeds.
            logger.info(
                "analyze_source_ineligible", extra={"workspace_id": ctx.workspace_id, "asset_version_id": asset_version_id},
            )
            return

        job = _find_job(
            session, ctx, "localization.analyze_source", "asset_version", asset_version_id,
            payload_filter={"batch_id": batch_id, "analysis_recipe_hash": analysis_recipe_hash},
        )
        if job is None:
            return
        batch_command = _find_batch_command_for_operation(session, ctx, job.operation_id)
        if batch_command is None:
            return

        if batch.provider_execution_mode != "mock":
            raise NotImplementedError(
                f"provider_execution_mode {batch.provider_execution_mode!r} has no dispatch "
                "implementation in this Worker slice"
            )

        recorder = _PreparationCallEvidenceRecorder(
            work_kind="analysis", batch=batch, batch_source=batch_source,
            batch_command_id=batch_command.id, analysis_recipe_hash=analysis_recipe_hash,
        )
        adapter = registry["analyze_source"]
        request = AnalyzeSourceRequest(asset_version_id=asset_version_id, analysis_recipe_hash=analysis_recipe_hash)

    result = await coordinator.execute(
        ctx, job_id=job.id, operation_id=job.operation_id,
        call_semantic_key_hash=_call_semantic_key(job),
        step_kind="source_analysis", execution_mode=batch.provider_execution_mode,
        chargeability="free_mock", provider=adapter.provider,
        request=request, adapter=adapter, evidence_recorder=recorder,
        spend_reservation_id=None, truth_package_id=batch.truth_package_id, model=None,
    )

    _settle_preparation_call(ctx, result, session_factory, work_kind="analysis")


# ---------------------------------------------------------------------------
# handle_translate_copy
# ---------------------------------------------------------------------------


async def handle_translate_copy(
    ctx: TenantContext,
    *,
    batch_id: str,
    asset_version_id: str,
    target_market: str,
    inventory_id: str,
    claim_decision_set_id: str,
    session_factory: SessionFactory,
    coordinator: ExternalCallCoordinator,
    adapter_registry: Mapping[str, object] | None = None,
) -> None:
    registry = adapter_registry or _DEFAULT_ADAPTER_REGISTRY

    with session_factory(ctx) as session:
        batch = _load_batch(session, ctx, batch_id)
        if batch is None or batch.phase in _TERMINAL_BATCH_PHASES:
            raise JobObsolete(
                "batch_terminal",
                "the Batch reached a terminal phase before this Job was claimed",
            )

        decision_set = session.get(batch_mod.LocalizationClaimDecisionSetRecord, claim_decision_set_id)
        if decision_set is None:
            return
        # Idempotent no-op: a translated CopyVersion already exists for
        # this exact recipe (reuse plan finding it moot to redispatch).
        existing = session.scalar(
            select(LocalizedCopyVersionRecord).where(
                LocalizedCopyVersionRecord.workspace_id == ctx.workspace_id,
                LocalizedCopyVersionRecord.claim_decision_set_id == claim_decision_set_id,
                LocalizedCopyVersionRecord.source_asset_version_id == asset_version_id,
                LocalizedCopyVersionRecord.target_market == target_market,
                LocalizedCopyVersionRecord.copy_origin == "translated",
            )
        )
        if existing is not None:
            return

        batch_source = session.scalar(
            select(LocalizationBatchSourceRecord).where(
                LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
                LocalizationBatchSourceRecord.batch_id == batch_id,
                LocalizationBatchSourceRecord.asset_version_id == asset_version_id,
            )
        )
        if batch_source is None:
            return
        batch_target = session.scalar(
            select(batch_mod.LocalizationBatchMarketTargetRecord).where(
                batch_mod.LocalizationBatchMarketTargetRecord.workspace_id
                == ctx.workspace_id,
                batch_mod.LocalizationBatchMarketTargetRecord.batch_id
                == batch_id,
                batch_mod.LocalizationBatchMarketTargetRecord.market
                == target_market,
            )
        )
        if batch_target is None:
            return

        report = validate_source_for_batch(
            session, ctx, asset_version_id, batch_source.decision_id,
            requested_evidence_tier=_requested_evidence_tier(batch),
        )
        if not report.valid:
            logger.info(
                "translate_copy_ineligible", extra={"workspace_id": ctx.workspace_id, "asset_version_id": asset_version_id},
            )
            return

        series_resource_id = batch_mod.copy_series_resource_id(
            asset_version_id, target_market, inventory_id, claim_decision_set_id,
        )
        job = _find_job(
            session, ctx, "localization.translate_copy", "copy_series", series_resource_id,
            payload_filter={"batch_id": batch_id},
        )
        if job is None:
            return
        batch_command = _find_batch_command_for_operation(session, ctx, job.operation_id)
        if batch_command is None:
            return

        if batch.provider_execution_mode != "mock":
            raise NotImplementedError(
                f"provider_execution_mode {batch.provider_execution_mode!r} has no dispatch "
                "implementation in this Worker slice"
            )

        prepared = prepare_copy_segments(
            session, ctx, inventory_id=inventory_id, decision_set_id=claim_decision_set_id,
        )
        segments = tuple((seg.claim_index, seg.text) for seg in prepared.translatable)

        market_policy_version_id = decision_set.market_policy_version_id
        truth_package_market_id = decision_set.truth_package_market_id

        recorder = _PreparationCallEvidenceRecorder(
            work_kind="translation", batch=batch, batch_source=batch_source,
            batch_command_id=batch_command.id, input_inventory_id=inventory_id,
            truth_package_market_id=truth_package_market_id, target_market=target_market,
            market_policy_version_id=market_policy_version_id, claim_decision_set_id=claim_decision_set_id,
            copy_kwargs=dict(
                source_asset_version_id=asset_version_id, inventory_id=inventory_id,
                truth_package_id=batch.truth_package_id, truth_package_market_id=truth_package_market_id,
                claim_decision_set_id=claim_decision_set_id, target_market=target_market,
                market_policy_version_id=market_policy_version_id, copy_policy_snapshot={},
            ),
        )
        adapter = registry["translate_copy"]
        _, target_locale = batch_mod._preparation_locales(
            session, batch_source, batch_target
        )
        request = TranslateCopyRequest(
            segments=segments, target_locale=target_locale
        )

    result = await coordinator.execute(
        ctx, job_id=job.id, operation_id=job.operation_id,
        call_semantic_key_hash=_call_semantic_key(job),
        step_kind="translation", execution_mode=batch.provider_execution_mode,
        chargeability="free_mock", provider=adapter.provider,
        request=request, adapter=adapter, evidence_recorder=recorder,
        spend_reservation_id=None, truth_package_id=batch.truth_package_id, model=None,
    )

    _settle_preparation_call(ctx, result, session_factory, work_kind="translation")


# ---------------------------------------------------------------------------
# handle_edit_attempt -- the primary edit dispatch
# ---------------------------------------------------------------------------


async def handle_edit_attempt(
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str,
    attempt_number: int,
    session_factory: SessionFactory,
    coordinator: ExternalCallCoordinator,
    adapter_registry: Mapping[str, object] | None = None,
) -> None:
    """Process one Attempt end to end: eligibility recheck -> dispatch ->
    persist isolated output -> eligibility recheck -> enqueue deterministic
    QC (spec §2G points 1-4)."""
    registry = adapter_registry or _DEFAULT_ADAPTER_REGISTRY

    with session_factory(ctx) as session:
        item = _load_item(session, ctx, item_id, batch_id)
        if item is None or item.state not in _EDIT_DISPATCHABLE_STATES:
            return
        if item.attempt_count != attempt_number:
            return
        batch = _load_batch(session, ctx, batch_id)
        if batch is None or batch.phase in _TERMINAL_BATCH_PHASES:
            raise JobObsolete(
                "batch_terminal",
                "the Batch reached a terminal phase before this Job was claimed",
            )
        attempt = _load_attempt_by_number(session, ctx, item_id, batch_id, attempt_number)

        if attempt is not None and attempt.state == "completed":
            # `resume_item()` (spec §2E: "continue only already-authorized
            # unspent work") re-queued this Item after a pause that was
            # discovered *after* this Attempt's edit call already
            # succeeded -- the output already exists, isolated (spec §2H:
            # "an already-dispatched call may complete ... output stays
            # isolated, never re-dispatched"). Never redispatch an edit
            # whose output already exists; re-drive the local, cost-free
            # QC/finalize pipeline forward instead by re-running the
            # idempotent deterministic-QC fold, which itself re-derives
            # whether to advance to external QC, retry_wait/failed, or
            # finalize.
            eligible, findings = _check_source_truth_copy_eligibility(session, ctx, item, batch)
            if not eligible:
                _pause_item(session, ctx, item, reason_code="ineligible_on_resume", findings=findings)
                return
            # `handle_run_deterministic_qc`/the placeholder QC handlers/
            # `handle_finalize_item` all guard on `item.state == "running"`
            # (spec §1.7's CHECK has no dedicated "QC in progress" value) --
            # `resume_item()` only ever leaves an eligible Item `queued`, so
            # this CAS transition is what actually re-enters the pipeline.
            item = _transition_item(session, ctx, item, state="running", clear_reason=True)
            enqueue_in_session(
                session, context=ctx, job_type="localization.run_deterministic_qc",
                resource_type="localization_item", resource_id=item_id,
                payload={"batch_id": batch_id, "item_id": item_id, "attempt_id": attempt.id},
                idempotency_key=f"detqc:{attempt.id}:resumed",
            )
            return

        if attempt is None or attempt.state not in ("authorized", "dispatching"):
            return

        # spec §4.2 / §2G point 2: recheck eligibility before every
        # external/paid step. Nothing has been dispatched yet -- ineligible
        # here means `paused`, never `failed` (no evidence exists to lose).
        eligible, findings = _check_source_truth_copy_eligibility(session, ctx, item, batch)
        if not eligible:
            _pause_item(session, ctx, item, reason_code="ineligible_before_dispatch", findings=findings)
            return
        copy_version = session.get(LocalizedCopyVersionRecord, item.copy_version_id)

        job = _find_job(
            session, ctx, "localization.edit_attempt", "localization_item", item_id,
            payload_filter={"attempt_number": attempt_number, "batch_id": batch_id},
        )
        if job is None:
            return
        batch_command = _find_batch_command_for_operation(session, ctx, job.operation_id)
        if batch_command is None:
            return

        if batch.provider_execution_mode != "mock":
            raise NotImplementedError(
                f"provider_execution_mode {batch.provider_execution_mode!r} has no dispatch "
                "implementation in this Worker slice"
            )

        if item.state != "running":
            item = _transition_item(session, ctx, item, state="running")

        source_av = session.get(AssetVersionRecord, item.source_asset_version_id)
        copy_content_hash = _hash(copy_version.content_json)
        request = EditAttemptRequest(
            item_id=item_id, attempt_number=attempt_number,
            source_object_key=source_av.object_key if source_av else "",
            copy_content_hash=copy_content_hash,
        )
        recorder = _EditCallEvidenceRecorder(item=item, attempt=attempt, batch_command_id=batch_command.id)
        adapter = registry["edit_attempt"]
        edit_model = (batch.frozen_recipe_json or {}).get("edit_model")
        if attempt.state == "authorized":
            attempt.state = "dispatching"
            session.flush()

    result = await coordinator.execute(
        ctx, job_id=job.id, operation_id=job.operation_id,
        call_semantic_key_hash=_call_semantic_key(job),
        step_kind="localization_edit", execution_mode=batch.provider_execution_mode,
        chargeability="free_mock", provider=adapter.provider,
        request=request, adapter=adapter, evidence_recorder=recorder,
        spend_reservation_id=None, truth_package_id=item.truth_package_id, model=edit_model,
    )

    # The queue outcome is decided inside the transaction below but raised
    # after it commits: signalling from inside the session would roll back
    # the very Item and Attempt evidence the signal reports.
    outcome_signal: HandlerOutcomeSignal | None = None

    with session_factory(ctx) as session:
        item = _load_item(session, ctx, item_id, batch_id)
        batch = _load_batch(session, ctx, batch_id)
        # `result.attempt_id` is the generic `ProviderCallAttemptRecord`'s
        # id, not this module's own `LocalizationAttemptRecord` -- reload
        # the latter by its natural (item_id, batch_id, attempt_number) key.
        attempt = _load_attempt_by_number(session, ctx, item_id, batch_id, attempt_number)
        if item is None or attempt is None or batch is None:
            return
        if item.state != "running":
            # Already advanced by a concurrent/earlier redelivery.
            return

        if not result.success:
            if result.dispatch_certainty == "uncertain":
                # spec §16.8: "Worker crash and uncertain must not repeat
                # external calls." A real call may already have been sent
                # (or even billed) -- this is neither a success nor a
                # genuine failure, so it must never consume a bounded
                # Attempt slot via `_handle_attempt_failure`. The Item
                # (and the Attempt header) sit in `uncertain` until an
                # explicit `ExternalCallCoordinator.reconcile()` pass
                # resolves it; nothing here auto-retries.
                attempt.state = "uncertain"
                session.flush()
                _transition_item(
                    session, ctx, item, state="uncertain",
                    reason_code="dispatch_uncertain",
                    reason_detail=result.error_detail or "call left uncertain; requires reconciliation",
                    next_action="reconcile",
                )
                outcome_signal = JobUncertain(
                    result.error_code or "edit_dispatch_uncertain",
                    result.error_detail
                    or "call left uncertain; requires reconciliation",
                )
            else:
                _handle_attempt_failure(
                    session, ctx, item, attempt, batch,
                    reason_code=result.error_code or "edit_dispatch_failed",
                    reason_detail=result.error_detail or "",
                )
                # The Item's own retry budget already absorbed this attempt
                # and, when one was left, enqueued the next one as its own
                # Job. Re-running *this* Job would redispatch an attempt the
                # domain has already accounted for, so the failure is
                # terminal for the Job even where the Item continues.
                outcome_signal = JobFailed(
                    result.error_code or "edit_dispatch_failed",
                    result.error_detail or "edit attempt dispatch failed",
                )
        else:
            # The external call itself is done and its output is already
            # persisted (isolated) by the coordinator's completion transaction
            # above -- `attempt.state` reflects that regardless of the
            # eligibility outcome below, so a later resume never mistakes this
            # completed, isolated-output Attempt for one still needing
            # dispatch (see the "already completed" branch at the top of this
            # function, which depends on exactly this).
            attempt.state = "completed"
            session.flush()

            # spec §2G point 4: recheck eligibility after the response, before
            # doing anything publishable. An ineligibility found here keeps the
            # output isolated rather than discarding it.
            report = validate_source_for_batch(
                session, ctx, item.source_asset_version_id, item.decision_id,
                requested_evidence_tier=_requested_evidence_tier(batch),
            )
            freshness = assess_truth_freshness(session, ctx, batch.truth_package_id, at=utcnow())
            if not (report.valid and freshness.is_current):
                _cancel_unstarted_jobs(session, ctx, resource_type="localization_item", resource_id=item_id)
                findings = list(report.findings) + list(freshness.reasons)
                _pause_item(session, ctx, item, reason_code="ineligible_after_dispatch", findings=findings)
            else:
                # `LocalizationItemRecord.state` has no dedicated "QC in
                # progress" value (spec §1.7's CHECK is queued/running/
                # retry_wait/paused/succeeded/failed/skipped/cancelled/
                # uncertain) -- the Item simply stays `running` through
                # deterministic + external QC until `handle_finalize_item`
                # moves it to `succeeded` or a QC/eligibility failure moves
                # it to `retry_wait`/`failed`/`paused`.
                enqueue_in_session(
                    session, context=ctx, job_type="localization.run_deterministic_qc",
                    resource_type="localization_item", resource_id=item_id,
                    payload={"batch_id": batch_id, "item_id": item_id, "attempt_id": attempt.id},
                    idempotency_key=f"detqc:{attempt.id}",
                )

    if outcome_signal is not None:
        raise outcome_signal


# ---------------------------------------------------------------------------
# handle_run_deterministic_qc
# ---------------------------------------------------------------------------


def _isolated_recovery_attempt(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str,
    attempt_id: str,
) -> LocalizationAttemptRecord | None:
    """The Attempt an authorized isolated-QC Job may run against.

    A recovery Job is admitted by `authorize_isolated_qc()` precisely
    *because* the Item is no longer running -- it was paused, cancelled or
    failed with its output preserved. The pipeline's `item.state ==
    'running'` guard would make every one of those Jobs a silent no-op, so
    the recovery path checks what actually matters instead: a completed
    Attempt that still holds an unpublished output.
    """
    item = _load_item(session, ctx, item_id, batch_id)
    if item is None:
        return None
    attempt = _load_attempt(session, ctx, attempt_id)
    if attempt is None or attempt.item_id != item_id or attempt.state != "completed":
        return None
    if _get_attempt_output(session, ctx, attempt_id) is None:
        return None
    return attempt


def handle_run_deterministic_qc(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str,
    attempt_id: str,
    isolated: bool = False,
) -> None:
    """Run (or re-run) the frozen deterministic plan for one Attempt.

    ``isolated`` marks a Job admitted by `authorize_isolated_qc()`. It runs
    the identical checks and writes the identical evidence, but it never
    touches the Item: the recovery path's publish decision belongs to the
    explicit `adopt_isolated_output()` command, not to this handler.
    Advancing the Item here would either resurrect a cancelled Item or
    consume an Attempt slot for work the user asked to re-check, not retry.
    """
    if isolated:
        attempt = _isolated_recovery_attempt(
            session, ctx, batch_id=batch_id, item_id=item_id, attempt_id=attempt_id
        )
        if attempt is None:
            return
        qc_mod.run_deterministic_qc(
            session, ctx, attempt_id=attempt_id, item_id=item_id, batch_id=batch_id
        )
        return

    item = _load_item(session, ctx, item_id, batch_id)
    if item is None or item.state != "running":
        return
    attempt = _load_attempt(session, ctx, attempt_id)
    if attempt is None or attempt.state != "completed":
        return

    report = qc_mod.run_deterministic_qc(session, ctx, attempt_id=attempt_id, item_id=item_id, batch_id=batch_id)
    batch = _load_batch(session, ctx, batch_id)

    if not report.all_blocking_passed:
        # spec §2G point 5: deterministic QC failure cancels avoidable
        # OCR/visual QC -- the Jobs batch.confirm()/retry_item() already
        # admitted for this Item's frozen external QC steps.
        _cancel_unstarted_jobs(session, ctx, resource_type="localization_item", resource_id=item_id)
        failing = [r.check_slot_key for r in report.records if r.severity == "blocking" and r.result == "fail"]
        _handle_attempt_failure(
            session, ctx, item, attempt, batch,
            reason_code="deterministic_qc_failed", reason_detail=",".join(failing),
        )
        return

    external_steps = (batch.frozen_recipe_json or {}).get("external_qc_steps") or []
    if not external_steps:
        _enqueue_finalize(session, ctx, item_id=item_id, batch_id=batch_id, attempt_id=attempt_id)


# ---------------------------------------------------------------------------
# handle_ocr_check / handle_visual_qc -- local placeholder QC (v1)
# ---------------------------------------------------------------------------


def _handle_placeholder_qc_step(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str,
    attempt_id: str,
    check_kind: str,
    isolated: bool = False,
) -> None:
    if isolated:
        attempt = _isolated_recovery_attempt(
            session, ctx, batch_id=batch_id, item_id=item_id, attempt_id=attempt_id
        )
        if attempt is None:
            return
        _record_placeholder_qc(
            session, ctx, batch_id=batch_id, item_id=item_id,
            attempt_id=attempt_id, check_kind=check_kind,
        )
        # Deliberately no finalize enqueue: the recovered output is
        # published by the explicit `adopt_isolated_output()` command, which
        # repeats every authoritative recheck first. Auto-finalizing here
        # would publish behind the user's back.
        return

    item = _load_item(session, ctx, item_id, batch_id)
    if item is None or item.state != "running":
        return
    attempt = _load_attempt(session, ctx, attempt_id)
    if attempt is None or attempt.state != "completed":
        return

    _record_placeholder_qc(
        session, ctx, batch_id=batch_id, item_id=item_id,
        attempt_id=attempt_id, check_kind=check_kind,
    )

    batch = _load_batch(session, ctx, batch_id)
    det_eligibility = qc_mod.derive_qc_eligibility(session, ctx, attempt_id=attempt_id, item_id=item_id, batch_id=batch_id)
    if det_eligibility.is_eligible and qc_mod.external_qc_steps_all_passed(session, ctx, batch=batch, attempt_id=attempt_id):
        _enqueue_finalize(session, ctx, item_id=item_id, batch_id=batch_id, attempt_id=attempt_id)


def _record_placeholder_qc(
    session: Session, ctx: TenantContext, *, batch_id: str, item_id: str,
    attempt_id: str, check_kind: str,
) -> None:
    """Append this attempt's placeholder QC evidence, once.

    `uq_lqr_attempt_slot` makes the row unique per (attempt, slot), so a
    redelivered Job -- or an isolated recovery re-running a check the
    pipeline already ran -- finds the existing record and writes nothing.
    """
    existing = session.scalar(
        select(LocalizationQcRecordRecord).where(
            LocalizationQcRecordRecord.workspace_id == ctx.workspace_id,
            LocalizationQcRecordRecord.attempt_id == attempt_id,
            LocalizationQcRecordRecord.check_slot_key == check_kind,
        )
    )
    if existing is not None:
        return
    # Deterministic Controlled Mock: always passes (spec brief: "OCR
    # verification step (placeholder/mock for v1)").
    session.add(
        LocalizationQcRecordRecord(
            id=new_id("lqr"), workspace_id=ctx.workspace_id, attempt_id=attempt_id,
            item_id=item_id, batch_id=batch_id, check_kind=check_kind,
            check_slot_key=check_kind, expected_json={"kind": check_kind},
            observed_json={"passed": True}, evidence_location_json=None,
            severity="blocking", result="pass", rule_version="placeholder-v1",
            created_at=utcnow(),
        )
    )
    session.flush()


def handle_ocr_check(
    session: Session, ctx: TenantContext, *, batch_id: str, item_id: str,
    attempt_id: str, isolated: bool = False,
) -> None:
    _handle_placeholder_qc_step(
        session, ctx, batch_id=batch_id, item_id=item_id, attempt_id=attempt_id,
        check_kind="ocr_check", isolated=isolated,
    )


def handle_visual_qc(
    session: Session, ctx: TenantContext, *, batch_id: str, item_id: str,
    attempt_id: str, isolated: bool = False,
) -> None:
    _handle_placeholder_qc_step(
        session, ctx, batch_id=batch_id, item_id=item_id, attempt_id=attempt_id,
        check_kind="visual_qc", isolated=isolated,
    )


def _enqueue_finalize(session: Session, ctx: TenantContext, *, item_id: str, batch_id: str, attempt_id: str) -> None:
    enqueue_in_session(
        session, context=ctx, job_type="localization.finalize_item",
        resource_type="localization_item", resource_id=item_id,
        payload={"batch_id": batch_id, "item_id": item_id, "attempt_id": attempt_id},
        idempotency_key=f"finalize:{item_id}:{attempt_id}",
    )


# ---------------------------------------------------------------------------
# handle_finalize_item
# ---------------------------------------------------------------------------


def handle_finalize_item(session: Session, ctx: TenantContext, *, batch_id: str, item_id: str, attempt_id: str) -> None:
    """Spec §2G: "Before finalize_item publishes, it repeats every
    authoritative check. It then atomically creates the child AssetVersion,
    localized lineage, active pending-review MarketAssetBinding, and Item
    success transition. The child starts with no human QualityReview and no
    Winner verdict. All publication evidence is written through the caller's
    transaction so a failed binding or Item transition rolls it all back.
    """
    item = _load_item(session, ctx, item_id, batch_id)
    if item is None or item.state != "running":
        return
    attempt = _load_attempt(session, ctx, attempt_id)
    if attempt is None or attempt.state != "completed":
        return
    output = _get_attempt_output(session, ctx, attempt_id)
    if output is None:
        return

    batch = _load_batch(session, ctx, batch_id)
    now = utcnow()

    report = validate_source_for_batch(
        session, ctx, item.source_asset_version_id, item.decision_id,
        requested_evidence_tier=_requested_evidence_tier(batch),
    )
    freshness = assess_truth_freshness(session, ctx, batch.truth_package_id, at=now)
    copy_version = session.get(LocalizedCopyVersionRecord, item.copy_version_id)
    copy_ok = copy_version is not None and copy_version.state == "approved"
    det_eligibility = qc_mod.derive_qc_eligibility(session, ctx, attempt_id=attempt_id, item_id=item_id, batch_id=batch_id)
    external_ok = qc_mod.external_qc_steps_all_passed(session, ctx, batch=batch, attempt_id=attempt_id)

    if not (report.valid and freshness.is_current and copy_ok and det_eligibility.is_eligible and external_ok):
        findings = (
            list(report.findings) + list(freshness.reasons)
            + list(det_eligibility.missing_slot_keys) + list(det_eligibility.blocking_failure_slot_keys)
        )
        if not copy_ok:
            findings.append("copy_not_approved")
        if not external_ok:
            findings.append("external_qc_incomplete")
        # spec §4.2: an already-dispatched call's output stays isolated,
        # never discarded -- the Item pauses with a precise recovery
        # action rather than losing the evidence.
        _pause_item(session, ctx, item, reason_code="ineligible_before_publish", findings=findings)
        return

    # The four publication writes and the binding live in
    # `app.localization.publication`, shared verbatim with
    # `adopt_isolated_output` so the recovered path cannot drift from this
    # one. All of it is written through this transaction: a failed binding
    # or Item transition rolls the child AssetVersion back with it.
    publication_mod.publish_localized_output(
        session,
        ctx,
        batch=batch,
        item=item,
        attempt=attempt,
        output=output,
        publication_kind="normal",
        actor_id=ctx.executor or "system:worker",
        role=ctx.initiator_role or "owner",
        now=now,
    )

    item = _transition_item(session, ctx, item, state="succeeded", clear_reason=True)

    enqueue_in_session(
        session, context=ctx, job_type="localization.fold_batch",
        resource_type="localization_batch", resource_id=batch_id,
        payload={"batch_id": batch_id},
        idempotency_key=f"fold:{batch_id}:{item_id}",
    )


# ---------------------------------------------------------------------------
# handle_fold_batch
# ---------------------------------------------------------------------------


def handle_fold_batch(session: Session, ctx: TenantContext, *, batch_id: str) -> None:
    """Derive the Batch's terminal outcome once every Item is terminal
    (spec §2F); a best-effort no-op otherwise or on a lost CAS race -- the
    next Item completion (or another `fold_batch` redelivery) recomputes
    the same projection."""
    batch = _load_batch(session, ctx, batch_id)
    if batch is None or batch.phase in _TERMINAL_BATCH_PHASES:
        return

    items = session.scalars(
        select(LocalizationItemRecord).where(
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.batch_id == batch_id,
        )
    ).all()
    if not items or any(i.state not in _TERMINAL_ITEM_STATES for i in items):
        return

    try:
        batch_mod.derive_batch_outcome(session, ctx, batch_id, expected_revision=batch.revision)
    except batch_mod.StaleRevisionError:
        logger.warning(
            "localization_batch_fold_conflict", extra={"workspace_id": ctx.workspace_id, "batch_id": batch_id},
        )
    except batch_mod.BatchNotTerminalError:
        # A concurrent writer moved an Item back out of terminal state
        # (e.g. a resume) between our read and `derive_batch_outcome`'s
        # own re-check -- nothing to do until the next fold.
        pass
