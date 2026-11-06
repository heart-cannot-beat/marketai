"""Localization batch module (#24, spec §2E/§2F/§2G, §3F, §16.2/§16.6/§16.8/§16.9).

Spec: docs/superpowers/specs/2026-09-06-localization-truth-bounded-qc-design.md
§2E ("Staged preparation and command admission" -- the full command
sequence), §2F ("app/localization/batch.py"), §2G (bounded QC/Attempts).

This module owns the complete Batch command sequence: ``create_draft`` ->
``start_preparation`` -> ``submit_claim_decisions`` -> ``approve_copy`` ->
``confirm`` -> ``retry_item``/``resume_item``/``authorize_isolated_qc``/
``adopt_isolated_output`` -> ``cancel``/``derive_batch_outcome``. Every
command follows the same shape: validate -> admit (only when the command
authorizes an external call, through
``app.entitlements.admission.PaidOperationAdmission.admit_in_transaction()``)
-> CAS transition -> persist -> return. Admission always runs *before* any
domain row for the command is created, so a failed admission (entitlement,
guardrail, capacity) leaves nothing for this command to roll back --
nothing was written yet (spec §3F: "entitlement, capacity or guardrail
failure -> no frozen Batch, command/Operation link, Attempt, Job or
Reservation commits").

Idempotency and replay (spec §3F, §16.8) are anchored on the append-only
``LocalizationBatchCommandRecord`` table: every command computes a
deterministic ``semantic_key_hash`` over its *business* inputs (never actor,
timestamp, or role) before doing any work. If a row with that hash already
exists for this Workspace, the call is a replay -- the original outcome is
reconstructed from already-persisted state, and neither Operation, Job,
Reservation, Attempt nor Item is duplicated. A different ``request_
fingerprint_hash`` under the same semantic key means the caller is trying
to rewrite the frozen intent of an already-issued command -- rejected as
``FrozenBatchError`` rather than silently reinterpreted.

``confirm()`` is the one-time freeze point (spec §11 冻结与幂等): once
``LocalizationBatchRecord.frozen_at`` is set, every subsequent attempt to
reach ``confirm()`` with different content raises ``FrozenBatchError``
without touching the frozen columns; only ``phase``, ``preparation_stage``,
``outcome``, ``truth_freshness``, ``revision`` and ``updated_at`` may still
change (a PostgreSQL trigger enforces the same rule at the database layer;
this module enforces it before ever attempting the write, on both engines).

``derive_batch_outcome()`` implements the rule from spec §2F: succeeded
when every Item succeeded or used ``skipped_no_change_required``; failed
when no market was executable; partially_succeeded otherwise. It runs only
once every Item is terminal.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Mapping, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    ExperimentDecisionRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionRecord,
    LibraryVersionRecord,
    LocalizationAttemptOutputRecord,
    LocalizationAttemptRecord,
    LocalizationBatchCommandRecord,
    LocalizationBatchMarketTargetRecord,
    LocalizationBatchRecord,
    LocalizationBatchSourceRecord,
    LocalizationClaimDecisionRecord,
    LocalizationClaimDecisionSetRecord,
    LocalizationItemRecord,
    LocalizationPreparationProviderCallRecord,
    LocalizedCopyApprovalRecord,
    LocalizedCopyVersionRecord,
    MarketAssetBindingRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    SourceClaimInventoryRecord,
    TruthPackageRecord,
    TruthPackageMarketRecord,
    new_id,
)
from ..entitlements.admission import PaidOperationAdmission
from ..entitlements.types import (
    AdmissionResult,
    AdmissionRequest,
    BillableCallSpec,
    EntitlementRequirements,
    ExecutionMode,
    JobSpec,
    OperationPayload,
    ProviderSelection,
)
from ..platform.jobs import enqueue_in_session
from ..platform.tenancy import TenantContext
from .claims import ClaimDecisionInput
from .claims import plan_inventories
from .claims import submit_claim_decisions as _claims_submit_claim_decisions
from .claim_facts import candidate_fact_types, required_fact_types
from .copy import (
    CopyPlanRequest,
    TranslationRecipe,
    approve_copy_version as _copy_approve_copy_version,
    create_source_locale_projection,
    plan_copy_versions,
)
from .eligibility import validate_source_for_batch
from .evidence_tier import EvidenceTier, derive_evidence_tier
from . import qc as qc_mod
from .source_context import resolve_source_market_snapshot
from .truth import (
    MarketResolutionInput,
    TruthResolutionCommand,
    assess_truth_freshness,
    prepare_truth_resolution_command,
    resolve_or_reuse_truth_package,
)

# A source AssetVersion's generation mode / an experiment's evidence mode use
# "byok|sandbox|mock" and "production|sandbox|mock" vocabularies respectively
# (spec §2A). `validate_source_for_batch`'s `requested_evidence_tier` always
# expects the latter; a Batch's `provider_execution_mode` is stored in the
# former. This is the single normalization point batch.py uses to bridge the
# two before calling into eligibility.py -- never re-derived ad hoc.
_PROVIDER_TO_EXPERIMENT_MODE = {"byok": "production", "sandbox": "sandbox", "mock": "mock"}

_TERMINAL_BATCH_PHASES = ("cancelled", "closed")
_TERMINAL_ITEM_STATES = ("succeeded", "failed", "skipped", "cancelled")
_ACTIVE_ITEM_STATES = ("queued", "running", "retry_wait", "paused", "uncertain")
_SOURCE_MARKET_SNAPSHOT_FIELDS = ("market", "placement", "locale", "country", "currency")


def _source_snapshot_findings(
    snapshot: Mapping[str, object], *, prefix: str
) -> list[str]:
    """Return stable validation findings for an explicit source-market snapshot.

    Locale, country and currency are business context, not values that can be
    inferred safely from an asset language or a default currency.  The API and
    the domain boundary therefore both require the complete snapshot.
    """
    findings: list[str] = []
    for field in _SOURCE_MARKET_SNAPSHOT_FIELDS:
        value = snapshot.get(field)
        if not isinstance(value, str) or not value.strip():
            findings.append(f"{prefix}:missing_source_{field}")
    return findings


def _analysis_recipe_state(
    batch: LocalizationBatchRecord,
) -> tuple[str | None, dict[str, str], tuple[str, ...]]:
    recipe = batch.frozen_recipe_json or {}
    recipe_hash = recipe.get("analysis_recipe_hash")
    inventory_ids = recipe.get("analysis_inventory_ids") or {}
    missing = recipe.get("analysis_missing_asset_version_ids") or []
    return (
        recipe_hash if isinstance(recipe_hash, str) else None,
        {
            str(asset_version_id): str(inventory_id)
            for asset_version_id, inventory_id in inventory_ids.items()
            if inventory_id
        }
        if isinstance(inventory_ids, dict)
        else {},
        tuple(str(asset_version_id) for asset_version_id in missing),
    )


def _bound_inventory_ids(
    session: Session,
    ctx: TenantContext,
    batch: LocalizationBatchRecord,
) -> dict[str, str]:
    """Resolve only inventories selected or produced for this exact Batch.

    Reused inventory ids are captured when preparation starts.  Missing
    analyses are bound by their persisted preparation work row when the worker
    commits its immutable result.  No lookup by "latest asset inventory" is
    permitted here.
    """
    _, inventory_ids, _ = _analysis_recipe_state(batch)
    work_rows = session.scalars(
        select(LocalizationPreparationProviderCallRecord)
        .where(
            LocalizationPreparationProviderCallRecord.workspace_id
            == ctx.workspace_id,
            LocalizationPreparationProviderCallRecord.batch_id == batch.id,
            LocalizationPreparationProviderCallRecord.work_kind == "analysis",
            LocalizationPreparationProviderCallRecord.state == "succeeded",
            LocalizationPreparationProviderCallRecord.result_inventory_id.is_not(None),
        )
        .order_by(LocalizationPreparationProviderCallRecord.created_at)
    ).all()
    for work in work_rows:
        if work.result_inventory_id is not None:
            inventory_ids.setdefault(
                work.source_asset_version_id, work.result_inventory_id
            )
    return inventory_ids


def _source_market_pair_key(asset_version_id: str, target_market: str) -> str:
    return f"{asset_version_id}:{target_market}"


def _preparation_locales(
    session: Session,
    source: LocalizationBatchSourceRecord,
    target: LocalizationBatchMarketTargetRecord,
) -> tuple[str, str]:
    """Derive copy locales only from the Batch's frozen server-side facts."""

    snapshot = source.source_market_snapshot_json or {}
    source_locale = snapshot.get("locale")
    policy = session.get(MarketPolicyVersionRecord, target.market_policy_version_id)
    target_locale = policy.locale if policy is not None else None
    findings: list[str] = []
    if not isinstance(source_locale, str) or not source_locale.strip():
        findings.append("source_locale_missing_from_frozen_snapshot")
    if not isinstance(target_locale, str) or not target_locale.strip():
        findings.append("target_locale_missing_from_market_policy")
    if findings:
        raise BatchValidationError(tuple(findings))
    return source_locale.strip(), target_locale.strip()


def _bound_decision_set_ids(
    batch: LocalizationBatchRecord,
) -> dict[str, str]:
    recipe = batch.frozen_recipe_json or {}
    values = recipe.get("claim_decision_set_ids") or {}
    if not isinstance(values, dict):
        return {}
    return {
        str(pair): str(decision_set_id)
        for pair, decision_set_id in values.items()
        if decision_set_id
    }


def _bound_copy_version_ids(
    session: Session,
    ctx: TenantContext,
    batch: LocalizationBatchRecord,
) -> dict[str, str]:
    """Return the exact CopyVersion selected or produced for each pair."""

    recipe = batch.frozen_recipe_json or {}
    values = recipe.get("copy_version_ids") or {}
    copy_ids = (
        {
            str(pair): str(copy_version_id)
            for pair, copy_version_id in values.items()
            if copy_version_id
        }
        if isinstance(values, dict)
        else {}
    )
    work_rows = session.scalars(
        select(LocalizationPreparationProviderCallRecord).where(
            LocalizationPreparationProviderCallRecord.workspace_id
            == ctx.workspace_id,
            LocalizationPreparationProviderCallRecord.batch_id == batch.id,
            LocalizationPreparationProviderCallRecord.work_kind == "translation",
            LocalizationPreparationProviderCallRecord.state == "succeeded",
            LocalizationPreparationProviderCallRecord.result_copy_version_id.is_not(
                None
            ),
        )
    ).all()
    for work in work_rows:
        if (
            work.source_asset_version_id
            and work.target_market
            and work.result_copy_version_id
        ):
            copy_ids.setdefault(
                _source_market_pair_key(
                    work.source_asset_version_id, work.target_market
                ),
                work.result_copy_version_id,
            )
    return copy_ids


def _bind_preparation_result(
    batch: LocalizationBatchRecord,
    *,
    asset_version_id: str,
    target_market: str,
    decision_set_id: str,
    copy_version_id: str | None,
) -> None:
    """Freeze pre-confirmation selections without replacing prior intent."""

    recipe = dict(batch.frozen_recipe_json or {})
    pair = _source_market_pair_key(asset_version_id, target_market)
    decision_ids = dict(recipe.get("claim_decision_set_ids") or {})
    existing_decision_id = decision_ids.get(pair)
    if existing_decision_id not in (None, decision_set_id):
        raise FrozenBatchError(
            f"batch {batch.id} already selected ClaimDecisionSet "
            f"{existing_decision_id} for {pair}"
        )
    decision_ids[pair] = decision_set_id
    recipe["claim_decision_set_ids"] = decision_ids

    if copy_version_id is not None:
        copy_ids = dict(recipe.get("copy_version_ids") or {})
        existing_copy_id = copy_ids.get(pair)
        if existing_copy_id not in (None, copy_version_id):
            raise FrozenBatchError(
                f"batch {batch.id} already selected CopyVersion "
                f"{existing_copy_id} for {pair}"
            )
        copy_ids[pair] = copy_version_id
        recipe["copy_version_ids"] = copy_ids
    batch.frozen_recipe_json = recipe


def _claim_batch_revision(
    session: Session,
    ctx: TenantContext,
    batch: LocalizationBatchRecord,
    *,
    expected_revision: int,
    now: datetime,
) -> None:
    """Claim one mutable Batch revision before appending preparation state."""

    claimed = session.execute(
        update(LocalizationBatchRecord)
        .where(
            LocalizationBatchRecord.id == batch.id,
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.revision == expected_revision,
        )
        .values(revision=expected_revision + 1, updated_at=now)
    )
    if claimed.rowcount != 1:
        raise StaleRevisionError("batch", batch.id, expected_revision)
    session.flush()
    session.refresh(batch)


def _refresh_preparation_projection(
    session: Session,
    ctx: TenantContext,
    batch: LocalizationBatchRecord,
    *,
    bump_revision_on_change: bool = False,
    now: datetime | None = None,
) -> bool:
    """Derive the durable preparation phase from exact bound identities.

    Request commands already claim a Batch revision before appending their
    evidence.  Background aggregation (notably translation completion) has
    no caller ETag, so it opts into ``bump_revision_on_change`` while holding
    the Batch row lock.  A no-op refresh never invalidates a client ETag.
    """

    if batch.frozen_at is not None or batch.phase in _TERMINAL_BATCH_PHASES:
        return False
    sources = session.scalars(
        select(LocalizationBatchSourceRecord).where(
            LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchSourceRecord.batch_id == batch.id,
        )
    ).all()
    targets = session.scalars(
        select(LocalizationBatchMarketTargetRecord).where(
            LocalizationBatchMarketTargetRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchMarketTargetRecord.batch_id == batch.id,
        )
    ).all()
    expected_pairs = {
        _source_market_pair_key(source.asset_version_id, target.market)
        for source in sources
        for target in targets
    }
    desired_phase = "preparing"
    desired_stage = "analysis"
    if len(_bound_inventory_ids(session, ctx, batch)) == len(sources):
        if _frozen_policy_coverage_findings(session, ctx, batch):
            desired_stage = "truth"
        else:
            decision_ids = _bound_decision_set_ids(batch)
            if not expected_pairs.issubset(decision_ids):
                desired_stage = "claim_review"
            else:
                copy_ids = _bound_copy_version_ids(session, ctx, batch)
                if not expected_pairs.issubset(copy_ids):
                    desired_stage = "copy"
                else:
                    copies = session.scalars(
                        select(LocalizedCopyVersionRecord).where(
                            LocalizedCopyVersionRecord.workspace_id
                            == ctx.workspace_id,
                            LocalizedCopyVersionRecord.id.in_(
                                [copy_ids[pair] for pair in sorted(expected_pairs)]
                            ),
                        )
                    ).all()
                    desired_phase = (
                        "ready"
                        if len(copies) == len(expected_pairs)
                        and all(copy.state == "approved" for copy in copies)
                        else "awaiting_copy_approval"
                    )
                    desired_stage = (
                        "complete" if desired_phase == "ready" else "copy"
                    )

    if (batch.phase, batch.preparation_stage) == (
        desired_phase,
        desired_stage,
    ):
        return False
    batch.phase = desired_phase
    batch.preparation_stage = desired_stage
    if bump_revision_on_change:
        batch.revision += 1
    batch.updated_at = now or datetime.now(timezone.utc)
    return True


def _policy_coverage_findings(
    session: Session,
    ctx: TenantContext,
    *,
    inventory_ids: Mapping[str, str],
    policy_fact_types: Sequence[str],
) -> tuple[str, ...]:
    """Validate one frozen policy against the exact selected inventories."""

    claim_types: list[object] = []
    for asset_version_id, inventory_id in sorted(inventory_ids.items()):
        inventory = session.scalar(
            select(SourceClaimInventoryRecord).where(
                SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
                SourceClaimInventoryRecord.id == inventory_id,
                SourceClaimInventoryRecord.asset_version_id == asset_version_id,
            )
        )
        if inventory is None:
            return (f"bound_inventory_missing:{asset_version_id}:{inventory_id}",)
        claim_types.extend(
            claim.get("claim_type")
            for claim in (inventory.claims_json or [])
            if isinstance(claim, dict)
        )

    required = set(required_fact_types(claim_types))
    available = set(policy_fact_types)
    return tuple(
        f"truth_policy_missing_fact_type:{fact_type}"
        for fact_type in sorted(required - available)
    )


def _frozen_policy_coverage_findings(
    session: Session,
    ctx: TenantContext,
    batch: LocalizationBatchRecord,
) -> tuple[str, ...]:
    recipe = batch.frozen_recipe_json or {}
    policy_fact_types = recipe.get("truth_policy_fact_types") or []
    if not isinstance(policy_fact_types, list):
        return ("invalid_frozen_truth_policy_fact_types",)
    return _policy_coverage_findings(
        session,
        ctx,
        inventory_ids=_bound_inventory_ids(session, ctx, batch),
        policy_fact_types=[str(value) for value in policy_fact_types],
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BatchError(RuntimeError):
    """Base for every error this module raises."""


class BatchValidationError(BatchError):
    """A command's synchronous validation/recheck failed. Findings are
    collected rather than short-circuited wherever practical, mirroring
    `app.localization.eligibility.validate_source_for_batch`."""

    def __init__(self, findings: Sequence[str]) -> None:
        self.findings = tuple(findings)
        super().__init__(";".join(self.findings) or "batch_validation_failed")


class BatchStateError(BatchError):
    """The Batch's current lifecycle phase does not admit this command."""


class StaleRevisionError(BatchError):
    """A CAS `UPDATE ... WHERE revision = expected_revision` affected no
    row (spec §3F: "all Item transitions use CAS and a losing writer
    reloads rather than overwriting") -- the same pattern
    `app.generation.draft.update_draft`/`app.generation.run_commands.
    _cas_bump_revision` use. The caller must reload the current row and
    retry with its current revision, never blindly resubmit the same
    `expected_revision`."""

    def __init__(self, record: str, record_id: str, expected_revision: int) -> None:
        self.record = record
        self.record_id = record_id
        self.expected_revision = expected_revision
        super().__init__(
            f"{record} {record_id} expected revision {expected_revision}, "
            "was concurrently modified"
        )


class FrozenBatchError(BatchError):
    """An attempt to rewrite a frozen field, or to reach `confirm()` again
    with different content after the Batch is already frozen (spec §11)."""


class AttemptExhaustedError(BatchError):
    """The Item has already used every permitted Attempt (spec §2E: "The
    hard cap is three image-edit Attempts per Item, including the first")."""

    def __init__(self, item_id: str, attempt_count: int) -> None:
        self.item_id = item_id
        self.attempt_count = attempt_count
        super().__init__(
            f"item {item_id} has exhausted its {attempt_count} permitted attempts"
        )


class ItemNotRetryableError(BatchError):
    def __init__(self, item_id: str, state: str) -> None:
        self.item_id = item_id
        self.state = state
        super().__init__(f"item {item_id} is not retryable from state {state!r}")


class BatchNotTerminalError(BatchError):
    def __init__(self, item_ids: Sequence[str]) -> None:
        self.item_ids = tuple(item_ids)
        super().__init__(f"items not yet terminal: {self.item_ids!r}")


# ---------------------------------------------------------------------------
# Value objects -- inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchSourceInput:
    """One selected Winner source for `create_draft()`."""

    asset_version_id: str
    decision_id: str
    # Deprecated compatibility input for direct service callers.  HTTP callers
    # cannot submit it; when present it is checked against, never used instead
    # of, the authoritative Winner-chain snapshot.
    source_market_snapshot: Mapping[str, object] | None = None


@dataclass(frozen=True)
class BatchMarketTargetInput:
    """One requested target market for `create_draft()`."""

    market_policy_version_id: str
    market: str


# ---------------------------------------------------------------------------
# Value objects -- results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchDraftResult:
    batch_id: str
    phase: str
    reused: bool


@dataclass(frozen=True)
class StartPreparationResult:
    batch_id: str
    truth_package_id: str
    truth_status: str
    preparation_stage: str
    operation_id: str | None
    missing_analysis: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True)
class SubmitClaimDecisionsResult:
    batch_id: str
    decision_set_id: str
    copy_version_id: str | None
    copy_origin: str  # "reused" | "source_locale_projection" | "translation_pending"
    operation_id: str | None
    replayed: bool


@dataclass(frozen=True)
class ApproveCopyResult:
    batch_id: str
    copy_version_id: str
    approval_id: str


@dataclass(frozen=True)
class ConfirmResult:
    batch_id: str
    item_ids: tuple[str, ...]
    operation_id: str | None
    replayed: bool


@dataclass(frozen=True)
class RetryItemResult:
    item_id: str
    attempt_number: int
    operation_id: str | None
    replayed: bool


@dataclass(frozen=True)
class ResumeItemResult:
    item_id: str
    state: str
    replayed: bool


@dataclass(frozen=True)
class AuthorizeIsolatedQCResult:
    item_id: str
    attempt_id: str
    operation_id: str | None
    replayed: bool
    #: The frozen external steps this command actually admitted -- empty
    #: when every step had already passed, which is also why there is no
    #: Operation in that case.
    authorized_steps: tuple[str, ...] = ()
    #: Whether the free, local deterministic plan also had to be re-run.
    deterministic_qc_enqueued: bool = False


@dataclass(frozen=True)
class AdoptIsolatedOutputResult:
    item_id: str
    attempt_id: str
    replayed: bool
    #: The published evidence, so the caller (and the browser) can reach the
    #: localized child without re-deriving it.
    asset_version_id: str = ""
    publication_id: str = ""
    binding_id: str = ""


@dataclass(frozen=True)
class CancelResult:
    batch_id: str
    cancelled_item_ids: tuple[str, ...]
    already_terminal: bool


# ---------------------------------------------------------------------------
# Internal working types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _JobPlan:
    job_type: str
    resource_type: str
    resource_id: str
    payload_json: dict
    call_semantic_key_hash: str
    provider: str
    model: str


@dataclass(frozen=True)
class _ItemPlan:
    source: LocalizationBatchSourceRecord
    target: LocalizationBatchMarketTargetRecord
    inventory_id: str
    truth_package_market_id: str
    decision_set_id: str
    copy_version_id: str
    initial_state: str
    reason_code: str | None
    reason_detail: str | None
    recoverable: bool | None
    next_action: str | None

    @property
    def executable(self) -> bool:
        return self.initial_state == "queued"


@dataclass(frozen=True)
class _ItemCreation:
    item_id: str
    plan: _ItemPlan
    item_fingerprint: str


# ---------------------------------------------------------------------------
# Hashing / canonicalization helpers
# ---------------------------------------------------------------------------


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# jobs.resource_id is varchar(100). A copy series is named by four ids
# (``<asset_version>:<market>:<inventory>:<decision_set>``), which is 113+
# characters for real ids; PostgreSQL rejects it (SQLite does not enforce the
# width). A series key that fits is stored unchanged; a longer one as a
# deterministic digest. The writer (translation admission) and the reader (the
# translate_copy handler finding its Job) must both go through this.
JOB_RESOURCE_ID_MAX_LENGTH = 100


def copy_series_resource_id(
    asset_version_id: str, target_market: str, inventory_id: str, decision_set_id: str,
) -> str:
    series_key = f"{asset_version_id}:{target_market}:{inventory_id}:{decision_set_id}"
    if len(series_key) <= JOB_RESOURCE_ID_MAX_LENGTH:
        return series_key
    return "sha256:" + hashlib.sha256(series_key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_batch(session: Session, ctx: TenantContext, batch_id: str) -> LocalizationBatchRecord:
    batch = session.scalar(
        select(LocalizationBatchRecord).where(
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.id == batch_id,
        )
    )
    if batch is None:
        raise BatchValidationError(("batch_not_found",))
    return batch


def _load_batch_for_update(
    session: Session, ctx: TenantContext, batch_id: str
) -> LocalizationBatchRecord:
    """Serialize commands that choose immutable preparation identities."""

    batch = session.scalar(
        select(LocalizationBatchRecord)
        .where(
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.id == batch_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise BatchValidationError(("batch_not_found",))
    return batch


def _load_item(
    session: Session, ctx: TenantContext, batch_id: str, item_id: str
) -> LocalizationItemRecord:
    item = session.scalar(
        select(LocalizationItemRecord).where(
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.id == item_id,
            LocalizationItemRecord.batch_id == batch_id,
        )
    )
    if item is None:
        raise BatchValidationError(("item_not_found",))
    return item


def _ensure_batch_active(batch: LocalizationBatchRecord) -> None:
    if batch.phase in _TERMINAL_BATCH_PHASES:
        raise BatchStateError(f"batch {batch.id} is {batch.phase}")


def _existing_command(
    session: Session, ctx: TenantContext, semantic_key_hash: str
) -> LocalizationBatchCommandRecord | None:
    return session.scalar(
        select(LocalizationBatchCommandRecord).where(
            LocalizationBatchCommandRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchCommandRecord.semantic_key_hash == semantic_key_hash,
        )
    )


def _record_command(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str | None,
    command_kind: str,
    operation_id: str | None,
    semantic_key_hash: str,
    request_fingerprint_hash: str,
    actor_id: str,
    now: datetime,
) -> None:
    session.add(
        LocalizationBatchCommandRecord(
            id=new_id("lbc"),
            workspace_id=ctx.workspace_id,
            batch_id=batch_id,
            item_id=item_id,
            command_kind=command_kind,
            operation_id=operation_id,
            semantic_key_hash=semantic_key_hash,
            request_fingerprint_hash=request_fingerprint_hash,
            actor_id=actor_id,
            created_at=now,
        )
    )


def _resolve_source_provenance(
    session: Session, ctx: TenantContext, decision_id: str
) -> tuple[ExperimentDecisionRecord, ExperimentVersionCandidateRecord]:
    """Load the Decision + Candidate for an already-validated source.

    Call only after `validate_source_for_batch` reports the source valid --
    validity already proves both rows exist and the Candidate's
    `asset_version_id` matches.
    """
    decision = session.scalar(
        select(ExperimentDecisionRecord).where(
            ExperimentDecisionRecord.workspace_id == ctx.workspace_id,
            ExperimentDecisionRecord.id == decision_id,
        )
    )
    candidate = session.scalar(
        select(ExperimentVersionCandidateRecord).where(
            ExperimentVersionCandidateRecord.workspace_id == ctx.workspace_id,
            ExperimentVersionCandidateRecord.id == decision.experiment_version_candidate_id,
        )
    )
    return decision, candidate


# ---------------------------------------------------------------------------
# Admission helper
# ---------------------------------------------------------------------------


def _admit_operation(
    session: Session,
    ctx: TenantContext,
    admission: PaidOperationAdmission,
    *,
    command_kind: str,
    semantic_key_hash: str,
    request_fingerprint_hash: str,
    jobs: Sequence[_JobPlan],
    execution_mode: str,
    result_resource_type: str,
    result_resource_id: str,
    feature_keys: tuple[str, ...] = (),
) -> AdmissionResult:
    """The only path to an Operation/Job/SpendReservation for a Batch
    command (spec §2E: "PaidOperationAdmission.admit_in_transaction()
    remains the only admission path for commands with external calls").
    Callers never invoke this with an empty `jobs` -- that case is handled
    by not fabricating an Operation at all (spec §3F).
    """
    job_specs = tuple(
        JobSpec(
            job_type=j.job_type,
            resource_type=j.resource_type,
            resource_id=j.resource_id,
            payload_json=j.payload_json,
            call_semantic_key_hash=j.call_semantic_key_hash,
        )
        for j in jobs
    )
    billable = tuple(
        BillableCallSpec(
            call_semantic_key_hash=j.call_semantic_key_hash,
            provider=j.provider,
            model=j.model,
            quantity=1,
        )
        for j in jobs
    )
    first = jobs[0]
    request = AdmissionRequest(
        command_type=f"localization.{command_kind}",
        semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash,
        entitlement_requirements=EntitlementRequirements(
            feature_keys=feature_keys, numeric_requirements=()
        ),
        provider_selection=ProviderSelection(
            execution_mode=ExecutionMode(execution_mode),
            provider=first.provider,
            model=first.model,
            binding_scope="workspace",
            credential_binding_id=None,
        ),
        billable_calls=billable,
        capacity_requests=(),
        operation_payload=OperationPayload(
            command_type=f"localization.{command_kind}",
            result_resource_type=result_resource_type,
            result_resource_id=result_resource_id,
            payload_version=1,
        ),
        job_specs=job_specs,
    )
    return admission.admit_in_transaction(session, ctx, request)


# ---------------------------------------------------------------------------
# create_draft
# ---------------------------------------------------------------------------


def create_draft(
    session: Session,
    ctx: TenantContext,
    *,
    product_version_id: str,
    offer_version_id: str,
    source_market_snapshot: Mapping[str, object] | None = None,
    truth_data_mode: Literal["real", "mock"],
    provider_execution_mode: Literal["byok", "sandbox", "mock"],
    sources: Sequence[BatchSourceInput],
    market_targets: Sequence[BatchMarketTargetInput],
    requested_evidence_tier: str | None = None,
    actor_id: str,
    now: datetime | None = None,
) -> BatchDraftResult:
    """`localization.create_draft` (spec §2E): "Persist selected sources
    and targets after discovery validation" -- no Operation.

    Enforces batch source consistency (spec §16.2: "a Batch cannot mix
    ProductVersion, OfferVersion, Workspace or source market"):
    ProductVersion/OfferVersion/Workspace are single values by
    construction (every lookup is scoped to `ctx.workspace_id`); every
    source's own `source_market_snapshot["market"]` must equal this
    Batch's shared source market.
    """
    now = now or datetime.now(timezone.utc)
    if not sources:
        raise BatchValidationError(("no_sources",))
    if not market_targets:
        raise BatchValidationError(("no_market_targets",))

    tier = requested_evidence_tier or _PROVIDER_TO_EXPERIMENT_MODE.get(
        provider_execution_mode, "mock"
    )

    findings: list[str] = []
    authoritative_source_snapshot: dict[str, str] | None = None
    seen_sources: set[str] = set()
    for source in sources:
        if source.asset_version_id in seen_sources:
            findings.append(f"source_{source.asset_version_id}:duplicate_source")
            continue
        seen_sources.add(source.asset_version_id)

        report = validate_source_for_batch(
            session, ctx, source.asset_version_id, source.decision_id,
            requested_evidence_tier=tier,
        )
        if not report.valid:
            findings.extend(
                f"source_{source.asset_version_id}:{f}" for f in report.findings
            )
        # spec §16.2: "a Batch cannot mix ProductVersion, OfferVersion,
        # Workspace or source market". `validate_source_for_batch` never
        # takes the Batch's own product_version_id/offer_version_id, so
        # this is the sole place that checks a source's *actual* Winner
        # context (Decision -> ExperimentVersion) against the Batch's
        # declared identity -- a source from a different Product/Offer
        # experiment must never silently join this Batch.
        decision = session.scalar(
            select(ExperimentDecisionRecord).where(
                ExperimentDecisionRecord.workspace_id == ctx.workspace_id,
                ExperimentDecisionRecord.id == source.decision_id,
            )
        )
        experiment_version = None
        if decision is not None and decision.experiment_version_id is not None:
            experiment_version = session.scalar(
                select(ExperimentVersionRecord).where(
                    ExperimentVersionRecord.workspace_id == ctx.workspace_id,
                    ExperimentVersionRecord.id == decision.experiment_version_id,
                )
            )
        if experiment_version is not None and (
            experiment_version.product_version_id != product_version_id
            or experiment_version.offer_version_id != offer_version_id
        ):
            findings.append(f"source_{source.asset_version_id}:product_offer_mismatch")
        if experiment_version is not None:
            offer_version = session.scalar(
                select(LibraryVersionRecord).where(
                    LibraryVersionRecord.id == experiment_version.offer_version_id,
                    LibraryVersionRecord.workspace_id == ctx.workspace_id,
                    LibraryVersionRecord.entry_type == "offer",
                )
            )
            if offer_version is None:
                findings.append(
                    f"source_{source.asset_version_id}:offer_version_missing"
                )
            else:
                resolved_snapshot, problem = resolve_source_market_snapshot(
                    experiment_version,
                    offer_version.content_json or {},
                )
                if problem is not None:
                    problem_fields = (
                        problem.get("missing_fields")
                        or problem.get("invalid_fields")
                        or problem.get("conflicting_fields")
                        or []
                    )
                    findings.append(
                        f"source_{source.asset_version_id}:{problem['code']}:"
                        f"{','.join(problem_fields)}"
                    )
                else:
                    assert resolved_snapshot is not None
                    if authoritative_source_snapshot is None:
                        authoritative_source_snapshot = resolved_snapshot
                    elif resolved_snapshot != authoritative_source_snapshot:
                        findings.append(
                            f"source_{source.asset_version_id}:"
                            "inconsistent_authoritative_source_market_snapshot"
                        )
                    if source.source_market_snapshot is not None and any(
                        source.source_market_snapshot.get(field)
                        != resolved_snapshot[field]
                        for field in _SOURCE_MARKET_SNAPSHOT_FIELDS
                    ):
                        findings.append(
                            f"source_{source.asset_version_id}:"
                            "submitted_source_market_snapshot_mismatch"
                        )

    if source_market_snapshot is not None and authoritative_source_snapshot is not None and any(
        source_market_snapshot.get(field)
        != authoritative_source_snapshot[field]
        for field in _SOURCE_MARKET_SNAPSHOT_FIELDS
    ):
        findings.append("batch:submitted_source_market_snapshot_mismatch")

    seen_markets: set[str] = set()
    for target in market_targets:
        if target.market in seen_markets:
            findings.append(f"market_{target.market}:duplicate_market_target")
            continue
        seen_markets.add(target.market)

        mpv = session.get(MarketPolicyVersionRecord, target.market_policy_version_id)
        if mpv is None or mpv.version_state != "effective":
            findings.append(f"market_{target.market}:invalid_market_policy_version")
            continue
        mp = session.get(MarketPolicyRecord, mpv.market_policy_id)
        if mp is None or mp.market_id != target.market:
            findings.append(f"market_{target.market}:market_mismatch")

    if findings:
        raise BatchValidationError(tuple(findings))
    if authoritative_source_snapshot is None:  # pragma: no cover - guarded above
        raise BatchValidationError(("authoritative_source_context_missing",))

    semantic_key_hash = _hash(
        {
            "cmd": "create_draft",
            "product_version_id": product_version_id,
            "offer_version_id": offer_version_id,
            "source_market_snapshot": authoritative_source_snapshot,
            "truth_data_mode": truth_data_mode,
            "provider_execution_mode": provider_execution_mode,
            "sources": sorted(
                (s.asset_version_id, s.decision_id) for s in sources
            ),
            "market_targets": sorted(
                (t.market, t.market_policy_version_id) for t in market_targets
            ),
        }
    )
    request_fingerprint_hash = semantic_key_hash

    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        batch = _load_batch(session, ctx, existing.batch_id)
        return BatchDraftResult(batch_id=batch.id, phase=batch.phase, reused=True)

    batch_id = new_id("lb")
    session.add(
        LocalizationBatchRecord(
            id=batch_id,
            workspace_id=ctx.workspace_id,
            product_version_id=product_version_id,
            offer_version_id=offer_version_id,
            source_market_snapshot_json=authoritative_source_snapshot,
            truth_package_id=None,
            truth_data_mode=truth_data_mode,
            provider_execution_mode=provider_execution_mode,
            evidence_tier=None,
            phase="draft",
            preparation_stage=None,
            outcome="none",
            truth_freshness=None,
            frozen_recipe_json=None,
            max_attempts=3,
            revision=1,
            frozen_at=None,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()

    for source in sources:
        decision, candidate = _resolve_source_provenance(session, ctx, source.decision_id)
        session.add(
            LocalizationBatchSourceRecord(
                id=new_id("lbs"),
                workspace_id=ctx.workspace_id,
                batch_id=batch_id,
                asset_version_id=source.asset_version_id,
                decision_id=source.decision_id,
                experiment_id=decision.experiment_id,
                experiment_version_id=decision.experiment_version_id,
                experiment_version_candidate_id=decision.experiment_version_candidate_id,
                source_generation_mode=candidate.execution_mode,
                experiment_evidence_mode=_PROVIDER_TO_EXPERIMENT_MODE.get(
                    candidate.execution_mode, "mock"
                ),
                source_market_snapshot_json=authoritative_source_snapshot,
                created_at=now,
            )
        )

    for target in market_targets:
        session.add(
            LocalizationBatchMarketTargetRecord(
                id=new_id("lbmt"),
                workspace_id=ctx.workspace_id,
                batch_id=batch_id,
                market_policy_version_id=target.market_policy_version_id,
                market=target.market,
                created_at=now,
            )
        )

    _record_command(
        session, ctx, batch_id=batch_id, item_id=None, command_kind="create_draft",
        operation_id=None, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return BatchDraftResult(batch_id=batch_id, phase="draft", reused=False)


# ---------------------------------------------------------------------------
# start_preparation
# ---------------------------------------------------------------------------


def _authoritative_truth_command(
    session: Session,
    ctx: TenantContext,
    *,
    batch: LocalizationBatchRecord,
    targets: Sequence[LocalizationBatchMarketTargetRecord],
    truth_resolution_policy_version_id: str,
    data_source_version_ids: tuple[str, ...],
    manual_fact_set_version_id: str | None,
    now: datetime,
) -> TruthResolutionCommand:
    """Build Truth input from persisted Batch and Library identities only."""

    return prepare_truth_resolution_command(
        session,
        ctx,
        product_version_id=batch.product_version_id,
        offer_version_id=batch.offer_version_id,
        truth_resolution_policy_version_id=truth_resolution_policy_version_id,
        data_source_version_ids=data_source_version_ids,
        markets=tuple(
            MarketResolutionInput(
                market_policy_version_id=target.market_policy_version_id
            )
            for target in sorted(targets, key=lambda item: item.market)
        ),
        manual_fact_set_version_id=manual_fact_set_version_id,
        allow_fallback=False,
        now=now,
    )


def _truth_selection(command: TruthResolutionCommand) -> dict[str, object]:
    source_version_ids = sorted(
        {
            source.data_source_version_id
            for market in command.markets
            for source in market.sources
        }
    )
    manual_version_ids = sorted(
        {
            source.manual_fact_set_version_id
            for market in command.markets
            for source in market.sources
            if source.manual_fact_set_version_id is not None
        }
    )
    return {
        "truth_resolution_policy_version_id": (
            command.truth_resolution_policy_version_id
        ),
        "truth_data_source_version_ids": source_version_ids,
        "truth_manual_fact_set_version_ids": manual_version_ids,
        "truth_policy_fact_types": sorted(
            {
                policy.fact_type
                for market in command.markets
                for policy in market.fact_policies
            }
        ),
    }


def start_preparation(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    truth_command: TruthResolutionCommand | None,
    analysis_recipe_hash: str,
    analysis_provider: str,
    analysis_model: str,
    analysis_adapter_version: str,
    actor_id: str,
    admission: PaidOperationAdmission,
    truth_resolution_policy_version_id: str | None = None,
    data_source_version_ids: tuple[str, ...] = (),
    manual_fact_set_version_id: str | None = None,
    now: datetime | None = None,
) -> StartPreparationResult:
    """`localization.start_preparation` (spec §2E): "Resolve/reuse Truth;
    reuse Inventories; start missing analysis" -- one `prepare_analysis`
    Operation with one Job per missing Inventory, or none at all when
    every Inventory is already reusable (spec §3F/§16.9).
    """
    now = now or datetime.now(timezone.utc)
    batch = _load_batch_for_update(session, ctx, batch_id)
    _ensure_batch_active(batch)

    sources = session.scalars(
        select(LocalizationBatchSourceRecord).where(
            LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchSourceRecord.batch_id == batch_id,
        )
    ).all()
    if not sources:
        raise BatchValidationError(("no_sources",))

    targets = session.scalars(
        select(LocalizationBatchMarketTargetRecord).where(
            LocalizationBatchMarketTargetRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchMarketTargetRecord.batch_id == batch_id,
        )
    ).all()
    if not targets:
        raise BatchValidationError(("no_market_targets",))

    bound_recipe_hash, bound_inventory_ids, original_missing = (
        _analysis_recipe_state(batch)
    )
    if truth_command is None:
        if truth_resolution_policy_version_id is None or not data_source_version_ids:
            raise BatchValidationError(("missing_truth_configuration_versions",))
        requested_selection: dict[str, object] = {
            "truth_resolution_policy_version_id": (
                truth_resolution_policy_version_id
            ),
            "truth_data_source_version_ids": sorted(data_source_version_ids),
            "truth_manual_fact_set_version_ids": (
                [manual_fact_set_version_id]
                if manual_fact_set_version_id is not None
                else []
            ),
        }
    else:
        requested_selection = _truth_selection(truth_command)

    # A replay is reconstructed from the already-frozen selection. It must
    # not re-read versions that may since have been superseded or withdrawn.
    if bound_recipe_hash is not None:
        if bound_recipe_hash != analysis_recipe_hash:
            raise FrozenBatchError(
                f"batch {batch_id} already selected analysis recipe "
                f"{bound_recipe_hash}"
            )
        selected_analysis = batch.frozen_recipe_json or {}
        requested_analysis = {
            "analysis_provider": analysis_provider,
            "analysis_model": analysis_model,
            "analysis_adapter_version": analysis_adapter_version,
        }
        if any(
            selected_analysis.get(field) != value
            for field, value in requested_analysis.items()
        ):
            raise FrozenBatchError(
                f"batch {batch_id} already selected different analysis execution"
            )
        if any(
            selected_analysis.get(field) != value
            for field, value in requested_selection.items()
        ):
            raise FrozenBatchError(
                f"batch {batch_id} already selected different Truth configuration"
            )
        existing = session.scalar(
            select(LocalizationBatchCommandRecord)
            .where(
                LocalizationBatchCommandRecord.workspace_id == ctx.workspace_id,
                LocalizationBatchCommandRecord.batch_id == batch_id,
                LocalizationBatchCommandRecord.command_kind == "start_preparation",
            )
            .order_by(LocalizationBatchCommandRecord.created_at)
        )
        if existing is None:
            raise FrozenBatchError(
                f"batch {batch_id} has an analysis recipe without a preparation command"
            )
        package = (
            session.get(TruthPackageRecord, batch.truth_package_id)
            if batch.truth_package_id is not None
            else None
        )
        if package is None or batch.truth_package_id is None:
            raise FrozenBatchError(
                f"batch {batch_id} has preparation state without frozen Truth"
            )
        return StartPreparationResult(
            batch_id=batch_id,
            truth_package_id=batch.truth_package_id,
            truth_status=package.status,
            preparation_stage=batch.preparation_stage,
            operation_id=existing.operation_id,
            missing_analysis=original_missing,
            replayed=True,
        )

    if truth_command is None:
        truth_command = _authoritative_truth_command(
            session,
            ctx,
            batch=batch,
            targets=targets,
            truth_resolution_policy_version_id=(
                truth_resolution_policy_version_id
            ),
            data_source_version_ids=tuple(data_source_version_ids),
            manual_fact_set_version_id=manual_fact_set_version_id,
            now=now,
        )
    if (
        truth_command.product_version_id != batch.product_version_id
        or truth_command.offer_version_id != batch.offer_version_id
    ):
        raise BatchValidationError(("truth_command_product_offer_mismatch",))
    if truth_command.truth_data_mode != batch.truth_data_mode:
        raise BatchValidationError(("truth_data_mode_mismatch",))
    selection = _truth_selection(truth_command)

    plan = plan_inventories(
        session, ctx,
        asset_version_ids=[s.asset_version_id for s in sources],
        analysis_recipe_hash=analysis_recipe_hash,
    )
    selected_inventory_ids = dict(bound_inventory_ids | dict(plan.reused))
    coverage_findings = _policy_coverage_findings(
        session,
        ctx,
        inventory_ids=selected_inventory_ids,
        policy_fact_types=selection["truth_policy_fact_types"],
    )
    if coverage_findings:
        raise BatchValidationError(coverage_findings)

    # Resolve only after the exact reusable inventory identities have been
    # selected. Missing analyses are later bound through their durable work
    # rows; the worker rechecks policy coverage before opening claim review.
    truth_result = resolve_or_reuse_truth_package(session, ctx, truth_command)

    if batch.truth_package_id is not None and batch.truth_package_id != truth_result.truth_package_id:
        raise FrozenBatchError(
            f"batch {batch_id} already resolved a different truth_package_id"
        )

    semantic_key_hash = _hash(
        {
            "cmd": "start_preparation",
            "batch_id": batch_id,
            "truth_package_id": truth_result.truth_package_id,
            "analysis_recipe_hash": analysis_recipe_hash,
            "analysis_inventory_ids": selected_inventory_ids,
            "missing": sorted(plan.missing),
        }
    )
    request_fingerprint_hash = _hash(
        {
            "semantic": semantic_key_hash,
            "analysis_provider": analysis_provider,
            "analysis_model": analysis_model,
            "analysis_adapter_version": analysis_adapter_version,
        }
    )

    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        if existing.request_fingerprint_hash != request_fingerprint_hash:
            raise FrozenBatchError(
                f"start_preparation command conflict for batch {batch_id}"
            )
        return StartPreparationResult(
            batch_id=batch_id, truth_package_id=truth_result.truth_package_id,
            truth_status=truth_result.status, preparation_stage=batch.preparation_stage,
            operation_id=existing.operation_id, missing_analysis=plan.missing, replayed=True,
        )
    if batch.frozen_at is not None:
        raise FrozenBatchError(f"batch {batch_id} is already frozen")

    operation_id: str | None = None
    if plan.missing:
        jobs = [
            _JobPlan(
                job_type="localization.analyze_source",
                resource_type="asset_version",
                resource_id=av_id,
                payload_json={
                    "batch_id": batch_id,
                    "asset_version_id": av_id,
                    "analysis_recipe_hash": analysis_recipe_hash,
                },
                call_semantic_key_hash=_hash(
                    {"batch_id": batch_id, "asset_version_id": av_id, "recipe": analysis_recipe_hash}
                ),
                provider=analysis_provider,
                model=analysis_model,
            )
            for av_id in plan.missing
        ]
        result = _admit_operation(
            session, ctx, admission,
            command_kind="start_preparation", semantic_key_hash=semantic_key_hash,
            request_fingerprint_hash=request_fingerprint_hash, jobs=jobs,
            execution_mode=batch.provider_execution_mode,
            result_resource_type="localization_batch", result_resource_id=batch_id,
        )
        operation_id = result.operation_id

    batch.truth_package_id = truth_result.truth_package_id
    batch.frozen_recipe_json = {
        "analysis_recipe_hash": analysis_recipe_hash,
        "analysis_provider": analysis_provider,
        "analysis_model": analysis_model,
        "analysis_adapter_version": analysis_adapter_version,
        "analysis_inventory_ids": selected_inventory_ids,
        "analysis_missing_asset_version_ids": list(plan.missing),
        **selection,
    }
    batch.phase = "preparing"
    batch.preparation_stage = "analysis" if plan.missing else "claim_review"
    batch.revision += 1
    batch.updated_at = now

    _record_command(
        session, ctx, batch_id=batch_id, item_id=None, command_kind="start_preparation",
        operation_id=operation_id, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return StartPreparationResult(
        batch_id=batch_id, truth_package_id=truth_result.truth_package_id,
        truth_status=truth_result.status, preparation_stage=batch.preparation_stage,
        operation_id=operation_id, missing_analysis=plan.missing, replayed=False,
    )


# ---------------------------------------------------------------------------
# submit_claim_decisions
# ---------------------------------------------------------------------------


def submit_claim_decisions(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    expected_revision: int,
    asset_version_id: str,
    target_market: str,
    analysis_recipe_hash: str,
    decisions: Sequence[ClaimDecisionInput],
    copy_policy_snapshot: Mapping[str, object],
    source_locale: str,
    target_locale: str,
    translation_recipe: TranslationRecipe | None,
    requested_evidence_tier: EvidenceTier,
    actor_id: str,
    role: str,
    admission: PaidOperationAdmission | None = None,
    at: datetime | None = None,
    now: datetime | None = None,
) -> SubmitClaimDecisionsResult:
    """`localization.submit_claim_decisions` (spec §2E): "Append
    confirmations and per-market decisions; plan/reuse/project copy; start
    missing translation" for one (source AssetVersion, target market)
    pair -- one `prepare_copy` Operation with one Job per missing
    translated CopyVersion, or none when the version is reused or produced
    as a deterministic source-locale projection.

    Blocks per spec §3F: analysis incomplete for this source, or an
    unresolved high-risk/conflicting/low-confidence Claim, rejects the
    whole command before any copy planning or admission is attempted --
    `app.localization.claims.submit_claim_decisions` itself never persists
    on an invalid submission.
    """
    now = now or datetime.now(timezone.utc)
    resolved_at = at or now
    batch = _load_batch(session, ctx, batch_id)
    _ensure_batch_active(batch)

    if batch.truth_package_id is None:
        raise BatchValidationError(("truth_not_resolved",))

    source = session.scalar(
        select(LocalizationBatchSourceRecord).where(
            LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchSourceRecord.batch_id == batch_id,
            LocalizationBatchSourceRecord.asset_version_id == asset_version_id,
        )
    )
    if source is None:
        raise BatchValidationError(("unknown_source",))

    target = session.scalar(
        select(LocalizationBatchMarketTargetRecord).where(
            LocalizationBatchMarketTargetRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchMarketTargetRecord.batch_id == batch_id,
            LocalizationBatchMarketTargetRecord.market == target_market,
        )
    )
    if target is None:
        raise BatchValidationError(("unknown_market_target",))

    server_source_locale, server_target_locale = _preparation_locales(
        session, source, target
    )
    locale_findings: list[str] = []
    if source_locale.strip().lower() != server_source_locale.lower():
        locale_findings.append("source_locale_mismatch")
    if target_locale.strip().lower() != server_target_locale.lower():
        locale_findings.append("target_locale_mismatch")
    if locale_findings:
        raise BatchValidationError(tuple(locale_findings))
    source_locale = server_source_locale
    target_locale = server_target_locale

    bound_recipe_hash, _, _ = _analysis_recipe_state(batch)
    if bound_recipe_hash is None:
        raise BatchValidationError(("analysis_recipe_not_bound",))
    if analysis_recipe_hash != bound_recipe_hash:
        raise FrozenBatchError(
            f"batch {batch_id} analysis recipe is {bound_recipe_hash}, not "
            f"{analysis_recipe_hash}"
        )
    inventory_id = _bound_inventory_ids(session, ctx, batch).get(asset_version_id)
    if inventory_id is None:
        raise BatchValidationError(("analysis_incomplete",))
    inventory = session.scalar(
        select(SourceClaimInventoryRecord).where(
            SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
            SourceClaimInventoryRecord.id == inventory_id,
            SourceClaimInventoryRecord.asset_version_id == asset_version_id,
            SourceClaimInventoryRecord.analysis_recipe_hash == bound_recipe_hash,
        )
    )
    if inventory is None:
        raise BatchValidationError(("bound_inventory_missing",))

    tpm = session.scalar(
        select(TruthPackageMarketRecord).where(
            TruthPackageMarketRecord.workspace_id == ctx.workspace_id,
            TruthPackageMarketRecord.truth_package_id == batch.truth_package_id,
            TruthPackageMarketRecord.market == target_market,
        )
    )
    if tpm is None:
        raise BatchValidationError(("truth_market_missing",))

    semantic_key_hash = _hash(
        {
            "cmd": "submit_claim_decisions",
            "batch_id": batch_id,
            "asset_version_id": asset_version_id,
            "target_market": target_market,
            "inventory_id": inventory_id,
            "decisions": sorted(
                (
                    d.claim_index, d.action, d.replacement_fact_id,
                    _canonical_json(d.removal_strategy) if d.removal_strategy else None,
                )
                for d in decisions
            ),
            "source_locale": source_locale,
            "target_locale": target_locale,
        }
    )
    request_fingerprint_hash = semantic_key_hash

    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        pair = _source_market_pair_key(asset_version_id, target_market)
        decision_set_id = _bound_decision_set_ids(batch).get(pair, "")
        copy_version_id = _bound_copy_version_ids(session, ctx, batch).get(pair)
        copy_version = (
            session.get(LocalizedCopyVersionRecord, copy_version_id)
            if copy_version_id
            else None
        )
        return SubmitClaimDecisionsResult(
            batch_id=batch_id,
            decision_set_id=decision_set_id,
            copy_version_id=copy_version.id if copy_version else None,
            copy_origin=(copy_version.copy_origin if copy_version else "translation_pending"),
            operation_id=existing.operation_id, replayed=True,
        )
    if batch.frozen_at is not None:
        raise FrozenBatchError(f"batch {batch_id} is already frozen")

    _claim_batch_revision(
        session, ctx, batch, expected_revision=expected_revision, now=now
    )

    decisions_result = _claims_submit_claim_decisions(
        session, ctx,
        origin_batch_id=batch_id, inventory_id=inventory_id,
        truth_package_id=batch.truth_package_id, truth_package_market_id=tpm.id,
        target_market=target_market, market_policy_version_id=target.market_policy_version_id,
        decisions=decisions, actor_id=actor_id, role=role, now=now,
    )
    if not decisions_result.valid:
        raise BatchValidationError(decisions_result.findings)

    decision_set_id = decisions_result.decision_set_id

    copy_plan = plan_copy_versions(
        session, ctx,
        requests=[
            CopyPlanRequest(
                key="only", source_asset_version_id=asset_version_id,
                inventory_id=inventory_id, truth_package_id=batch.truth_package_id,
                truth_package_market_id=tpm.id, claim_decision_set_id=decision_set_id,
                target_market=target_market, market_policy_version_id=target.market_policy_version_id,
                source_locale=source_locale, target_locale=target_locale,
                copy_policy_snapshot=copy_policy_snapshot, translation_recipe=translation_recipe,
            )
        ],
        requested_evidence_tier=requested_evidence_tier, at=resolved_at,
    )

    copy_version_id: str | None = None
    copy_origin = "translation_pending"
    operation_id: str | None = None

    if "only" in copy_plan.reused:
        copy_version_id = copy_plan.reused["only"]
        copy_version = session.get(LocalizedCopyVersionRecord, copy_version_id)
        copy_origin = copy_version.copy_origin if copy_version else "reused"
    elif "only" in copy_plan.source_locale_projections:
        created = create_source_locale_projection(
            session, ctx, source_asset_version_id=asset_version_id, inventory_id=inventory_id,
            truth_package_id=batch.truth_package_id, truth_package_market_id=tpm.id,
            claim_decision_set_id=decision_set_id, target_market=target_market,
            market_policy_version_id=target.market_policy_version_id,
            copy_policy_snapshot=copy_policy_snapshot, at=resolved_at, now=now,
        )
        copy_version_id = created.copy_version.id
        copy_origin = "source_locale_projection"
    else:
        if translation_recipe is None:
            raise BatchValidationError(("missing_translation_recipe",))
        if admission is None:
            raise BatchValidationError(("admission_required_for_translation",))
        series_key = f"{asset_version_id}:{target_market}:{inventory_id}:{decision_set_id}"
        jobs = [
            _JobPlan(
                job_type="localization.translate_copy",
                resource_type="copy_series",
                resource_id=copy_series_resource_id(
                    asset_version_id, target_market, inventory_id, decision_set_id,
                ),
                payload_json={
                    "batch_id": batch_id, "asset_version_id": asset_version_id,
                    "target_market": target_market, "inventory_id": inventory_id,
                    "claim_decision_set_id": decision_set_id,
                },
                call_semantic_key_hash=_hash({"series": series_key, "recipe": translation_recipe}),
                provider=translation_recipe.provider, model=translation_recipe.model,
            )
        ]
        result = _admit_operation(
            session, ctx, admission,
            command_kind="submit_claim_decisions", semantic_key_hash=semantic_key_hash,
            request_fingerprint_hash=request_fingerprint_hash, jobs=jobs,
            execution_mode=batch.provider_execution_mode,
            result_resource_type="localization_batch", result_resource_id=batch_id,
        )
        operation_id = result.operation_id
        copy_origin = "translation_pending"

    _bind_preparation_result(
        batch,
        asset_version_id=asset_version_id,
        target_market=target_market,
        decision_set_id=decision_set_id,
        copy_version_id=copy_version_id,
    )
    _refresh_preparation_projection(session, ctx, batch)
    batch.updated_at = now

    _record_command(
        session, ctx, batch_id=batch_id, item_id=None, command_kind="submit_claim_decisions",
        operation_id=operation_id, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return SubmitClaimDecisionsResult(
        batch_id=batch_id, decision_set_id=decision_set_id, copy_version_id=copy_version_id,
        copy_origin=copy_origin, operation_id=operation_id, replayed=False,
    )


# ---------------------------------------------------------------------------
# approve_copy
# ---------------------------------------------------------------------------


def approve_copy(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    expected_revision: int,
    copy_version_id: str,
    actor_id: str,
    role: str,
    now: datetime | None = None,
) -> ApproveCopyResult:
    """`localization.approve_copy` (spec §2E): "Append Owner/Member copy
    approvals" -- no Operation.

    The semantic key is computed from business inputs -- `batch_id`,
    `source_asset_version_id`/`target_market` (read off the CopyVersion
    itself), `copy_version_id`, *and* `actor_id` -- and checked via
    `_existing_command` *before* `copy.approve_copy_version` is ever
    called. `actor_id` is deliberately included here, unlike every other
    command's semantic key: the spec's copy-approval model is multiple
    *distinct* Owner/Member approvers each appending their own
    `LocalizedCopyApprovalRecord` for the same CopyVersion (`copy.
    approve_copy_version`: "a later approval ... still appends its own
    record (e.g. a second required approver)"). Excluding `actor_id`
    would give a second approver's genuinely new approval the same hash
    as the first approver's, and `_existing_command` would silently treat
    it as a replay -- dropping a real, distinct approval. Including it
    keeps the same actor calling twice idempotent (replay) while still
    letting a different actor's approval through as new work. Computing
    the hash from a freshly-minted `approval.id` (as an earlier revision
    of this function did) can never match a prior command at all, so
    *every* replayed call -- same actor or not -- would append a
    duplicate row; checking first and only calling the approval function
    on a genuine miss is what makes this command idempotent per actor.
    """
    now = now or datetime.now(timezone.utc)
    batch = _load_batch(session, ctx, batch_id)
    _ensure_batch_active(batch)

    copy_version = session.scalar(
        select(LocalizedCopyVersionRecord).where(
            LocalizedCopyVersionRecord.workspace_id == ctx.workspace_id,
            LocalizedCopyVersionRecord.id == copy_version_id,
        )
    )
    if copy_version is None:
        raise BatchValidationError(("missing_copy_version",))
    if copy_version_id not in set(
        _bound_copy_version_ids(session, ctx, batch).values()
    ):
        raise BatchValidationError(("copy_not_bound_to_batch",))

    semantic_key_hash = _hash(
        {
            "cmd": "approve_copy", "batch_id": batch_id,
            "source_asset_version_id": copy_version.source_asset_version_id,
            "target_market": copy_version.target_market,
            "copy_version_id": copy_version_id,
            "actor_id": actor_id,
        }
    )

    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        approval = session.scalar(
            select(LocalizedCopyApprovalRecord)
            .where(
                LocalizedCopyApprovalRecord.workspace_id == ctx.workspace_id,
                LocalizedCopyApprovalRecord.copy_version_id == copy_version_id,
                LocalizedCopyApprovalRecord.actor_id == actor_id,
            )
            .order_by(LocalizedCopyApprovalRecord.created_at.desc())
        )
        return ApproveCopyResult(
            batch_id=batch_id, copy_version_id=copy_version_id,
            approval_id=approval.id if approval is not None else "",
        )

    _claim_batch_revision(
        session, ctx, batch, expected_revision=expected_revision, now=now
    )

    approval = _copy_approve_copy_version(
        session, ctx, copy_version_id=copy_version_id, actor_id=actor_id, role=role, now=now,
    )

    _refresh_preparation_projection(session, ctx, batch)
    batch.updated_at = now

    _record_command(
        session, ctx, batch_id=batch_id, item_id=None, command_kind="approve_copy",
        operation_id=None, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=semantic_key_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return ApproveCopyResult(batch_id=batch_id, copy_version_id=copy_version_id, approval_id=approval.id)


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def _truth_failure_code(tpm: TruthPackageMarketRecord) -> str:
    detail = tpm.readiness_detail_json or {}
    reason = str(detail.get("reason") or "market_failed")
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in reason.lower()
    ).strip("_")
    return f"truth_{normalized or 'market_failed'}"[:60]


def _price_omission_is_executable(
    session: Session,
    *,
    inventory: SourceClaimInventoryRecord,
    decision_set: LocalizationClaimDecisionSetRecord,
    market_policy_version: MarketPolicyVersionRecord,
) -> bool:
    """Apply the contract's sole ``failed``-Truth execution exception.

    A skipped target price can proceed only when the MarketPolicy does not
    require a local price and every source Claim that could carry a price was
    explicitly removed.  No empty/missing decision is treated as removal.
    """

    pricing_context = market_policy_version.pricing_context_json or {}
    if bool(pricing_context.get("requires_local_price", False)):
        return False

    price_claim_indexes = {
        index
        for index, claim in enumerate(inventory.claims_json or [])
        if isinstance(claim, dict)
        and "price" in candidate_fact_types(str(claim.get("claim_type") or ""))
    }
    decisions = session.scalars(
        select(LocalizationClaimDecisionRecord).where(
            LocalizationClaimDecisionRecord.workspace_id == inventory.workspace_id,
            LocalizationClaimDecisionRecord.decision_set_id == decision_set.id,
        )
    ).all()
    removed_indexes = {
        decision.claim_index
        for decision in decisions
        if decision.action == "remove"
    }
    return price_claim_indexes.issubset(removed_indexes)


def _pair_initial_disposition(
    session: Session,
    *,
    tpm: TruthPackageMarketRecord,
    inventory: SourceClaimInventoryRecord,
    decision_set: LocalizationClaimDecisionSetRecord,
    market_policy_version: MarketPolicyVersionRecord,
) -> tuple[str, str | None, str | None, bool | None, str | None]:
    """Project one frozen source×market pair without hiding bad Truth."""

    detail = dict(tpm.readiness_detail_json or {})
    rendered_detail = _canonical_json(
        {"market": tpm.market, "readiness": tpm.readiness, **detail}
    )[:500]
    if tpm.readiness in {"ready", "degraded"}:
        return "queued", None, None, None, None
    if tpm.readiness == "unavailable":
        return (
            "skipped",
            "skipped_market_unavailable",
            rendered_detail,
            False,
            None,
        )
    if (
        tpm.readiness == "failed"
        and detail.get("reason") == "price_unresolved"
        and _price_omission_is_executable(
            session,
            inventory=inventory,
            decision_set=decision_set,
            market_policy_version=market_policy_version,
        )
    ):
        return "queued", None, None, None, None
    return (
        "failed",
        _truth_failure_code(tpm),
        rendered_detail,
        False,
        "create_new_batch",
    )


def confirm(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    expected_revision: int,
    edit_provider: str,
    edit_model: str,
    edit_adapter_version: str,
    external_qc_steps: Sequence[str] = (),
    max_attempts: int = 3,
    actor_id: str,
    role: str,
    admission: PaidOperationAdmission,
    now: datetime | None = None,
) -> ConfirmResult:
    """`localization.confirm` (spec §2E): "Recheck and freeze; create
    Items/Attempt 1; authorize edit and configured external QC calls" --
    one Operation; one Job (and, when chargeable, one SpendReservation)
    per external call.

    This is the one-time freeze point (spec §11, §3F): every frozen field
    (`product_version_id`, `offer_version_id`, `source_market_snapshot_json`,
    `truth_package_id`, `truth_data_mode`, `provider_execution_mode`,
    `evidence_tier`, `frozen_recipe_json`, `max_attempts`) is set exactly
    once, in the same transaction that creates Items and Attempt 1 and
    admits their Jobs. Admission runs before any of those rows are
    created, so a failed admission leaves nothing to undo.

    The freezing write itself is a real CAS `UPDATE ... WHERE revision =
    expected_revision` (spec §3F: "all Item transitions use CAS and a
    losing writer reloads rather than overwriting" -- the same pattern
    `app.generation.draft.update_draft`/`app.generation.run_commands.
    _cas_bump_revision` use). A losing writer's `expected_revision` no
    longer matches, the UPDATE affects no row, and `StaleRevisionError`
    propagates -- which, inside the caller's `tenant_session`, rolls back
    everything this call admitted, exactly like an entitlement failure.
    """
    now = now or datetime.now(timezone.utc)
    batch = _load_batch(session, ctx, batch_id)
    _ensure_batch_active(batch)

    if batch.truth_package_id is None:
        raise BatchValidationError(("truth_not_resolved",))
    freshness = assess_truth_freshness(session, ctx, batch.truth_package_id, at=now)
    if not freshness.is_current:
        raise BatchValidationError(tuple(f"truth_stale:{r}" for r in freshness.reasons))
    truth_package = session.scalar(
        select(TruthPackageRecord).where(
            TruthPackageRecord.workspace_id == ctx.workspace_id,
            TruthPackageRecord.id == batch.truth_package_id,
        )
    )
    if truth_package is None:
        raise BatchValidationError(("truth_package_missing",))
    if truth_package.status == "failed":
        raise BatchValidationError(("truth_package_failed",))

    sources = session.scalars(
        select(LocalizationBatchSourceRecord).where(
            LocalizationBatchSourceRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchSourceRecord.batch_id == batch_id,
        )
    ).all()
    targets = session.scalars(
        select(LocalizationBatchMarketTargetRecord).where(
            LocalizationBatchMarketTargetRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchMarketTargetRecord.batch_id == batch_id,
        )
    ).all()
    if not sources or not targets:
        raise BatchValidationError(("no_sources_or_targets",))

    bound_recipe_hash, _, _ = _analysis_recipe_state(batch)
    if bound_recipe_hash is None:
        raise BatchValidationError(("analysis_recipe_not_bound",))
    bound_inventory_ids = _bound_inventory_ids(session, ctx, batch)
    bound_decision_set_ids = _bound_decision_set_ids(batch)
    bound_copy_version_ids = _bound_copy_version_ids(session, ctx, batch)

    requested_tier = _PROVIDER_TO_EXPERIMENT_MODE.get(batch.provider_execution_mode, "mock")

    findings: list[str] = []
    combos: list[_ItemPlan] = []
    for source in sources:
        report = validate_source_for_batch(
            session, ctx, source.asset_version_id, source.decision_id,
            requested_evidence_tier=requested_tier,
        )
        if not report.valid:
            findings.extend(f"source_{source.asset_version_id}:{f}" for f in report.findings)
            continue

        inventory_id = bound_inventory_ids.get(source.asset_version_id)
        inventory_row = session.scalar(
            select(SourceClaimInventoryRecord).where(
                SourceClaimInventoryRecord.workspace_id == ctx.workspace_id,
                SourceClaimInventoryRecord.id == inventory_id,
                SourceClaimInventoryRecord.asset_version_id == source.asset_version_id,
                SourceClaimInventoryRecord.analysis_recipe_hash == bound_recipe_hash,
            )
        ) if inventory_id is not None else None
        if inventory_row is None:
            findings.append(f"source_{source.asset_version_id}:analysis_incomplete")
            continue

        for target in targets:
            pair_key = _source_market_pair_key(
                source.asset_version_id, target.market
            )
            tpm = session.scalar(
                select(TruthPackageMarketRecord).where(
                    TruthPackageMarketRecord.workspace_id == ctx.workspace_id,
                    TruthPackageMarketRecord.truth_package_id == batch.truth_package_id,
                    TruthPackageMarketRecord.market == target.market,
                )
            )
            if tpm is None:
                findings.append(f"target_{target.market}:truth_market_missing")
                continue
            market_policy_version = session.get(
                MarketPolicyVersionRecord, target.market_policy_version_id
            )
            if market_policy_version is None:
                findings.append(
                    f"target_{target.market}:market_policy_version_missing"
                )
                continue

            decision_set_id = bound_decision_set_ids.get(pair_key)
            decision_set = session.scalar(
                select(LocalizationClaimDecisionSetRecord)
                .where(
                    LocalizationClaimDecisionSetRecord.workspace_id == ctx.workspace_id,
                    LocalizationClaimDecisionSetRecord.id == decision_set_id,
                    LocalizationClaimDecisionSetRecord.inventory_id == inventory_row.id,
                    LocalizationClaimDecisionSetRecord.truth_package_id == batch.truth_package_id,
                    LocalizationClaimDecisionSetRecord.truth_package_market_id == tpm.id,
                    LocalizationClaimDecisionSetRecord.target_market == target.market,
                    LocalizationClaimDecisionSetRecord.market_policy_version_id
                    == target.market_policy_version_id,
                )
            )
            if decision_set is None:
                findings.append(
                    f"source_{source.asset_version_id}_market_{target.market}:claim_decisions_missing"
                )
                continue

            copy_version_id = bound_copy_version_ids.get(pair_key)
            copy_version = session.scalar(
                select(LocalizedCopyVersionRecord)
                .where(
                    LocalizedCopyVersionRecord.workspace_id == ctx.workspace_id,
                    LocalizedCopyVersionRecord.id == copy_version_id,
                    LocalizedCopyVersionRecord.source_asset_version_id == source.asset_version_id,
                    LocalizedCopyVersionRecord.inventory_id == inventory_row.id,
                    LocalizedCopyVersionRecord.truth_package_id == batch.truth_package_id,
                    LocalizedCopyVersionRecord.truth_package_market_id == tpm.id,
                    LocalizedCopyVersionRecord.claim_decision_set_id == decision_set.id,
                    LocalizedCopyVersionRecord.target_market == target.market,
                    LocalizedCopyVersionRecord.market_policy_version_id
                    == target.market_policy_version_id,
                    LocalizedCopyVersionRecord.state == "approved",
                )
            )
            if copy_version is None:
                findings.append(
                    f"source_{source.asset_version_id}_market_{target.market}:copy_not_approved"
                )
                continue

            disposition = _pair_initial_disposition(
                session,
                tpm=tpm,
                inventory=inventory_row,
                decision_set=decision_set,
                market_policy_version=market_policy_version,
            )

            combos.append(
                _ItemPlan(
                    source=source, target=target, inventory_id=inventory_row.id,
                    truth_package_market_id=tpm.id, decision_set_id=decision_set.id,
                    copy_version_id=copy_version.id,
                    initial_state=disposition[0],
                    reason_code=disposition[1],
                    reason_detail=disposition[2],
                    recoverable=disposition[3],
                    next_action=disposition[4],
                )
            )

    if findings:
        raise BatchValidationError(tuple(findings))
    if not combos:
        raise BatchValidationError(("no_executable_items",))

    evidence_tier = derive_evidence_tier(
        source_modes=[s.source_generation_mode for s in sources],
        experiment_modes=[s.experiment_evidence_mode for s in sources],
        truth_data_mode=batch.truth_data_mode, provider_mode=batch.provider_execution_mode,
    )

    frozen_recipe = {
        **(batch.frozen_recipe_json or {}),
        "analysis_recipe_hash": bound_recipe_hash,
        "analysis_inventory_ids": dict(bound_inventory_ids),
        "edit_provider": edit_provider, "edit_model": edit_model,
        "edit_adapter_version": edit_adapter_version,
        "external_qc_steps": sorted(external_qc_steps),
    }

    item_creations: list[_ItemCreation] = []
    for combo in sorted(combos, key=lambda c: (c.source.asset_version_id, c.target.market)):
        item_id = new_id("li")
        item_fingerprint = _hash(
            {
                "source_asset_version_id": combo.source.asset_version_id,
                "decision_id": combo.source.decision_id,
                "offer_version_id": batch.offer_version_id,
                "target_market": combo.target.market,
                "truth_package_id": batch.truth_package_id,
                "inventory_id": combo.inventory_id,
                "copy_version_id": combo.copy_version_id,
                "market_policy_version_id": combo.target.market_policy_version_id,
                "frozen_recipe": frozen_recipe,
                "provider_execution_mode": batch.provider_execution_mode,
            }
        )
        item_creations.append(
            _ItemCreation(item_id=item_id, plan=combo, item_fingerprint=item_fingerprint)
        )

    semantic_key_hash = _hash(
        {
            "cmd": "confirm", "batch_id": batch_id, "frozen_recipe": frozen_recipe,
            "max_attempts": max_attempts,
            "item_fingerprints": sorted(c.item_fingerprint for c in item_creations),
        }
    )
    request_fingerprint_hash = _hash({"semantic": semantic_key_hash, "actor_id": actor_id})

    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        if existing.request_fingerprint_hash != request_fingerprint_hash:
            raise FrozenBatchError(f"confirm command conflict for batch {batch_id}")
        existing_items = session.scalars(
            select(LocalizationItemRecord).where(
                LocalizationItemRecord.workspace_id == ctx.workspace_id,
                LocalizationItemRecord.batch_id == batch_id,
            )
        ).all()
        return ConfirmResult(
            batch_id=batch_id, item_ids=tuple(i.id for i in existing_items),
            operation_id=existing.operation_id, replayed=True,
        )
    if batch.frozen_at is not None:
        raise FrozenBatchError(f"batch {batch_id} is already frozen")

    jobs: list[_JobPlan] = []
    for creation in item_creations:
        if creation.plan.initial_state != "queued":
            continue
        jobs.append(
            _JobPlan(
                job_type="localization.edit_attempt", resource_type="localization_item",
                resource_id=creation.item_id,
                payload_json={"attempt_number": 1, "batch_id": batch_id},
                call_semantic_key_hash=_hash({"item": creation.item_id, "attempt": 1, "step": "edit"}),
                provider=edit_provider, model=edit_model,
            )
        )
        for step in sorted(external_qc_steps):
            jobs.append(
                _JobPlan(
                    job_type=f"localization.{step}", resource_type="localization_item",
                    resource_id=creation.item_id,
                    payload_json={"attempt_number": 1, "batch_id": batch_id, "step": step},
                    call_semantic_key_hash=_hash({"item": creation.item_id, "attempt": 1, "step": step}),
                    provider=edit_provider, model=edit_model,
                )
            )

    admission_result = _admit_operation(
        session, ctx, admission,
        command_kind="confirm", semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash, jobs=jobs,
        execution_mode=batch.provider_execution_mode,
        result_resource_type="localization_batch", result_resource_id=batch_id,
    )

    cas_result = session.execute(
        update(LocalizationBatchRecord)
        .where(
            LocalizationBatchRecord.id == batch_id,
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.revision == expected_revision,
        )
        .values(
            truth_freshness="current",
            evidence_tier=evidence_tier.name.lower(),
            frozen_recipe_json=frozen_recipe,
            max_attempts=max_attempts,
            frozen_at=now,
            phase="running",
            preparation_stage="complete",
            revision=expected_revision + 1,
            updated_at=now,
        )
    )
    if cas_result.rowcount != 1:
        raise StaleRevisionError("batch", batch_id, expected_revision)
    session.flush()
    session.refresh(batch)

    item_ids: list[str] = []
    for creation in item_creations:
        combo = creation.plan
        item = LocalizationItemRecord(
            id=creation.item_id, workspace_id=ctx.workspace_id, batch_id=batch_id,
            batch_source_id=combo.source.id, batch_market_target_id=combo.target.id,
            source_asset_version_id=combo.source.asset_version_id,
            decision_id=combo.source.decision_id, target_market=combo.target.market,
            market_policy_version_id=combo.target.market_policy_version_id,
            truth_package_id=batch.truth_package_id,
            truth_package_market_id=combo.truth_package_market_id,
            inventory_id=combo.inventory_id, claim_decision_set_id=combo.decision_set_id,
            copy_version_id=combo.copy_version_id, state=combo.initial_state,
            reason_code=combo.reason_code, reason_detail=combo.reason_detail,
            recoverable=combo.recoverable, next_action=combo.next_action,
            attempt_count=1 if combo.initial_state == "queued" else 0,
            item_fingerprint_hash=creation.item_fingerprint,
            revision=1, created_at=now, updated_at=now,
        )
        session.add(item)
        session.flush()
        item_ids.append(item.id)

        if combo.initial_state == "queued":
            session.add(
                LocalizationAttemptRecord(
                    id=new_id("la"), workspace_id=ctx.workspace_id,
                    item_id=item.id, batch_id=batch_id,
                    attempt_number=1, provider=edit_provider, model=edit_model,
                    prompt_params_hash=_hash(
                        {"edit_adapter_version": edit_adapter_version,
                         "recipe": frozen_recipe}
                    ),
                    input_versions_hash=_hash(
                        {
                            "source_asset_version_id": combo.source.asset_version_id,
                            "copy_version_id": combo.copy_version_id,
                            "truth_package_id": batch.truth_package_id,
                        }
                    ),
                    state="authorized", created_at=now,
                )
            )

    _record_command(
        session, ctx, batch_id=batch_id, item_id=None, command_kind="confirm",
        operation_id=admission_result.operation_id, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return ConfirmResult(
        batch_id=batch_id, item_ids=tuple(item_ids),
        operation_id=admission_result.operation_id, replayed=False,
    )


# ---------------------------------------------------------------------------
# retry_item
# ---------------------------------------------------------------------------


def retry_item(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str,
    expected_item_revision: int,
    actor_id: str,
    role: str,
    admission: PaidOperationAdmission,
    now: datetime | None = None,
) -> RetryItemResult:
    """`localization.retry_item` (spec §2E): "Explicitly authorize Attempt
    2 or 3 after a failed Attempt" -- one Operation; one Job (and, when
    chargeable, one SpendReservation) per external call. Reuses the
    frozen edit/QC recipe from `confirm()` -- retry re-quotes and
    rechecks the guardrail, it never renegotiates the recipe itself
    (spec §2E).

    The Item transition is a real CAS `UPDATE ... WHERE revision =
    expected_item_revision` (spec §3F); a losing writer's stale
    `expected_item_revision` raises `StaleRevisionError` instead of
    silently overwriting a concurrent transition.
    """
    now = now or datetime.now(timezone.utc)
    batch = _load_batch(session, ctx, batch_id)
    _ensure_batch_active(batch)
    item = _load_item(session, ctx, batch_id, item_id)

    if item.attempt_count >= batch.max_attempts:
        raise AttemptExhaustedError(item_id, item.attempt_count)
    if item.state != "retry_wait":
        raise ItemNotRetryableError(item_id, item.state)

    freshness = assess_truth_freshness(session, ctx, batch.truth_package_id, at=now)
    if not freshness.is_current:
        raise BatchValidationError(tuple(f"truth_stale:{r}" for r in freshness.reasons))
    report = validate_source_for_batch(
        session, ctx, item.source_asset_version_id, item.decision_id,
        requested_evidence_tier=_PROVIDER_TO_EXPERIMENT_MODE.get(batch.provider_execution_mode, "mock"),
    )
    if not report.valid:
        raise BatchValidationError(tuple(f"source:{f}" for f in report.findings))

    frozen_recipe = batch.frozen_recipe_json or {}
    attempt_number = item.attempt_count + 1

    semantic_key_hash = _hash(
        {"cmd": "retry_item", "item_id": item_id, "attempt_number": attempt_number}
    )
    request_fingerprint_hash = _hash({"semantic": semantic_key_hash, "actor_id": actor_id})

    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        if existing.request_fingerprint_hash != request_fingerprint_hash:
            raise FrozenBatchError(f"retry_item command conflict for item {item_id}")
        return RetryItemResult(
            item_id=item_id, attempt_number=attempt_number,
            operation_id=existing.operation_id, replayed=True,
        )

    edit_provider = frozen_recipe.get("edit_provider", "unknown")
    edit_model = frozen_recipe.get("edit_model", "unknown")
    edit_adapter_version = frozen_recipe.get("edit_adapter_version", "unknown")
    external_qc_steps = frozen_recipe.get("external_qc_steps", [])

    jobs = [
        _JobPlan(
            job_type="localization.edit_attempt", resource_type="localization_item",
            resource_id=item_id,
            payload_json={"attempt_number": attempt_number, "batch_id": batch_id},
            call_semantic_key_hash=_hash({"item": item_id, "attempt": attempt_number, "step": "edit"}),
            provider=edit_provider, model=edit_model,
        )
    ]
    for step in sorted(external_qc_steps):
        jobs.append(
            _JobPlan(
                job_type=f"localization.{step}", resource_type="localization_item",
                resource_id=item_id,
                payload_json={"attempt_number": attempt_number, "batch_id": batch_id, "step": step},
                call_semantic_key_hash=_hash({"item": item_id, "attempt": attempt_number, "step": step}),
                provider=edit_provider, model=edit_model,
            )
        )

    admission_result = _admit_operation(
        session, ctx, admission,
        command_kind="retry_item", semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash, jobs=jobs,
        execution_mode=batch.provider_execution_mode,
        result_resource_type="localization_item", result_resource_id=item_id,
    )

    session.add(
        LocalizationAttemptRecord(
            id=new_id("la"), workspace_id=ctx.workspace_id, item_id=item_id, batch_id=batch_id,
            attempt_number=attempt_number, provider=edit_provider, model=edit_model,
            prompt_params_hash=_hash(
                {"edit_adapter_version": edit_adapter_version, "recipe": frozen_recipe}
            ),
            input_versions_hash=_hash(
                {
                    "source_asset_version_id": item.source_asset_version_id,
                    "copy_version_id": item.copy_version_id,
                    "truth_package_id": item.truth_package_id,
                    "attempt_number": attempt_number,
                }
            ),
            state="authorized", created_at=now,
        )
    )
    cas_result = session.execute(
        update(LocalizationItemRecord)
        .where(
            LocalizationItemRecord.id == item_id,
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.batch_id == batch_id,
            LocalizationItemRecord.revision == expected_item_revision,
        )
        .values(
            attempt_count=attempt_number, state="queued", reason_code=None,
            reason_detail=None, recoverable=None, next_action=None,
            revision=expected_item_revision + 1, updated_at=now,
        )
    )
    if cas_result.rowcount != 1:
        raise StaleRevisionError("item", item_id, expected_item_revision)
    session.flush()
    session.refresh(item)

    _record_command(
        session, ctx, batch_id=batch_id, item_id=item_id, command_kind="retry_item",
        operation_id=admission_result.operation_id, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return RetryItemResult(
        item_id=item_id, attempt_number=attempt_number,
        operation_id=admission_result.operation_id, replayed=False,
    )


# ---------------------------------------------------------------------------
# resume_item
# ---------------------------------------------------------------------------


def resume_item(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str,
    expected_item_revision: int,
    actor_id: str,
    now: datetime | None = None,
) -> ResumeItemResult:
    """`localization.resume_item` (spec §2E): "Recheck a paused Item and
    continue only already-authorized unspent work" -- no new paid call;
    if more paid work is required, the caller must use the applicable
    explicit command (`retry_item`/`authorize_isolated_qc`) instead.

    The Item transition is a real CAS `UPDATE ... WHERE revision =
    expected_item_revision` (spec §3F), matching `retry_item`.
    """
    now = now or datetime.now(timezone.utc)
    batch = _load_batch(session, ctx, batch_id)
    item = _load_item(session, ctx, batch_id, item_id)

    if item.state != "paused":
        raise ItemNotRetryableError(item_id, item.state)

    semantic_key_hash = _hash(
        {"cmd": "resume_item", "item_id": item_id, "revision": expected_item_revision}
    )
    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        return ResumeItemResult(item_id=item_id, state=item.state, replayed=True)

    report = validate_source_for_batch(
        session, ctx, item.source_asset_version_id, item.decision_id,
        requested_evidence_tier=_PROVIDER_TO_EXPERIMENT_MODE.get(batch.provider_execution_mode, "mock"),
    )
    freshness = assess_truth_freshness(session, ctx, batch.truth_package_id, at=now)
    eligible = report.valid and freshness.is_current

    if eligible:
        values = {
            "state": "queued", "reason_code": None, "reason_detail": None,
            "next_action": None,
        }
    else:
        values = {
            "state": "cancelled", "reason_code": "ineligible_on_resume",
            "reason_detail": ";".join(report.findings + freshness.reasons) or None,
            "recoverable": False, "next_action": "create_new_batch",
        }
    values["revision"] = expected_item_revision + 1
    values["updated_at"] = now

    cas_result = session.execute(
        update(LocalizationItemRecord)
        .where(
            LocalizationItemRecord.id == item_id,
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.batch_id == batch_id,
            LocalizationItemRecord.revision == expected_item_revision,
        )
        .values(**values)
    )
    if cas_result.rowcount != 1:
        raise StaleRevisionError("item", item_id, expected_item_revision)
    session.flush()
    session.refresh(item)

    _record_command(
        session, ctx, batch_id=batch_id, item_id=item_id, command_kind="resume_item",
        operation_id=None, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=semantic_key_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return ResumeItemResult(item_id=item_id, state=item.state, replayed=False)


# ---------------------------------------------------------------------------
# authorize_isolated_qc
# ---------------------------------------------------------------------------


def authorize_isolated_qc(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str,
    attempt_id: str,
    actor_id: str,
    role: str,
    admission: PaidOperationAdmission | None = None,
    now: datetime | None = None,
) -> AuthorizeIsolatedQCResult:
    """`localization.authorize_isolated_qc` (spec §2E/§2H): "Explicitly
    authorize missing QC for a preserved isolated output" -- one
    Operation only when external QC remains; there is no QC waiver.

    Only the frozen external steps this Attempt has *not* already passed are
    admitted. Re-authorizing the whole frozen plan would re-run every check
    the Attempt already cleared, and for a chargeable step that is a second
    reservation and a second bill for evidence that is already durable.
    """
    now = now or datetime.now(timezone.utc)
    batch = _load_batch(session, ctx, batch_id)
    _load_item(session, ctx, batch_id, item_id)

    attempt = session.scalar(
        select(LocalizationAttemptRecord).where(
            LocalizationAttemptRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptRecord.id == attempt_id,
            LocalizationAttemptRecord.item_id == item_id,
        )
    )
    if attempt is None:
        raise BatchValidationError(("attempt_not_found",))
    output = session.scalar(
        select(LocalizationAttemptOutputRecord).where(
            LocalizationAttemptOutputRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptOutputRecord.attempt_id == attempt_id,
        )
    )
    if output is None:
        raise BatchValidationError(("no_isolated_output",))

    missing_steps = qc_mod.missing_external_qc_steps(
        session, ctx, batch=batch, attempt_id=attempt_id
    )
    # The deterministic plan can be incomplete too -- an Item cancelled or
    # paused before its free, local QC ran keeps an output whose
    # deterministic slots were never filled. Nothing else will ever fill
    # them (the pipeline handlers only advance a `running` Item), so
    # adoption would be refused forever. These checks cost nothing and call
    # no provider, so they need no Operation and no reservation.
    deterministic = qc_mod.derive_qc_eligibility(
        session, ctx, attempt_id=attempt_id, item_id=item_id, batch_id=batch_id
    )
    deterministic_incomplete = bool(deterministic.missing_slot_keys)

    # The semantic key names the work this command actually authorizes, so
    # a later call that finds a different gap outstanding is a different
    # command rather than a replay of this one.
    semantic_key_hash = _hash(
        {
            "cmd": "authorize_isolated_qc",
            "attempt_id": attempt_id,
            "steps": sorted(missing_steps),
            "deterministic": deterministic_incomplete,
        }
    )
    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        return AuthorizeIsolatedQCResult(
            item_id=item_id, attempt_id=attempt_id,
            operation_id=existing.operation_id, replayed=True,
            authorized_steps=tuple(sorted(missing_steps)),
            deterministic_qc_enqueued=deterministic_incomplete,
        )

    if deterministic_incomplete:
        enqueue_in_session(
            session,
            context=ctx,
            job_type="localization.run_deterministic_qc",
            resource_type="localization_attempt",
            resource_id=attempt_id,
            payload={
                "batch_id": batch_id,
                "item_id": item_id,
                "attempt_id": attempt_id,
                "isolated": True,
            },
            idempotency_key=f"detqc:isolated:{attempt_id}",
        )

    operation_id: str | None = None
    if missing_steps:
        if admission is None:
            raise BatchValidationError(("admission_required_for_external_qc",))
        frozen_recipe = batch.frozen_recipe_json or {}
        jobs = [
            _JobPlan(
                job_type=f"localization.{step}", resource_type="localization_attempt",
                resource_id=attempt_id,
                # `resource_id` is the *Attempt* for these Jobs, so the
                # Worker cannot derive the Item from it the way it does for
                # the Item-scoped QC Jobs `confirm()` admits. Every
                # identifier the handler needs is therefore carried
                # explicitly in the payload.
                payload_json={
                    "batch_id": batch_id,
                    "item_id": item_id,
                    "attempt_id": attempt_id,
                    "step": step,
                    # Marks a recovery Job: the handler checks the Attempt
                    # rather than the Item's state, and never advances the
                    # Item -- publication stays with `adopt_isolated_output`.
                    "isolated": True,
                },
                call_semantic_key_hash=_hash(
                    {"attempt": attempt_id, "step": step, "cmd": "authorize_isolated_qc"}
                ),
                provider=frozen_recipe.get("edit_provider", "unknown"),
                model=frozen_recipe.get("edit_model", "unknown"),
            )
            for step in sorted(missing_steps)
        ]
        result = _admit_operation(
            session, ctx, admission,
            command_kind="authorize_isolated_qc", semantic_key_hash=semantic_key_hash,
            request_fingerprint_hash=semantic_key_hash, jobs=jobs,
            execution_mode=batch.provider_execution_mode,
            result_resource_type="localization_attempt", result_resource_id=attempt_id,
        )
        operation_id = result.operation_id

    _record_command(
        session, ctx, batch_id=batch_id, item_id=item_id, command_kind="authorize_isolated_qc",
        operation_id=operation_id, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=semantic_key_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return AuthorizeIsolatedQCResult(
        item_id=item_id, attempt_id=attempt_id, operation_id=operation_id, replayed=False,
        authorized_steps=tuple(sorted(missing_steps)),
        deterministic_qc_enqueued=deterministic_incomplete,
    )


# ---------------------------------------------------------------------------
# adopt_isolated_output
# ---------------------------------------------------------------------------


def adopt_isolated_output(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    item_id: str,
    attempt_id: str,
    actor_id: str,
    role: str,
    now: datetime | None = None,
) -> AdoptIsolatedOutputResult:
    """`localization.adopt_isolated_output` (spec §2E/§2H): "Recheck and
    publish an isolated output whose entire frozen QC plan has passed" --
    no external call and no new Operation.

    "Publish" is the operative word. This command previously flipped the
    Item to `succeeded` and stopped, which reported a recovered output as
    published while no AssetVersion, lineage or binding existed anywhere --
    the Item claimed a result that could not be opened, exported or bound to
    a market. Adoption now goes through the same
    `app.localization.publication.publish_localized_output` the normal
    finalize path uses, in this transaction, so a failure anywhere in the
    sequence rolls the whole adoption back.

    There is no `qc_passed` shortcut. Every frozen check -- the
    deterministic plan *and* the external steps -- must have recorded a pass
    for this exact Attempt; a step that was never run is refused with
    `qc_plan_incomplete` and has to go through `authorize_isolated_qc()`
    first. A blocking check that ran and *failed* can never be adopted at
    all.
    """
    from . import publication as publication_mod

    now = now or datetime.now(timezone.utc)
    batch = _load_batch(session, ctx, batch_id)
    item = _load_item(session, ctx, batch_id, item_id)

    attempt = session.scalar(
        select(LocalizationAttemptRecord).where(
            LocalizationAttemptRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptRecord.id == attempt_id,
            LocalizationAttemptRecord.item_id == item_id,
        )
    )
    if attempt is None:
        raise BatchValidationError(("attempt_not_found",))
    output = session.scalar(
        select(LocalizationAttemptOutputRecord).where(
            LocalizationAttemptOutputRecord.workspace_id == ctx.workspace_id,
            LocalizationAttemptOutputRecord.attempt_id == attempt_id,
        )
    )
    if output is None:
        raise BatchValidationError(("no_isolated_output",))

    # Replay before the state guard below: a redelivered adoption of an
    # output this command already published is a replay, not an error, and
    # it must return the same evidence rather than `already_adopted`.
    semantic_key_hash = _hash({"cmd": "adopt_isolated_output", "attempt_id": attempt_id})
    request_fingerprint_hash = _hash({"semantic": semantic_key_hash, "actor_id": actor_id})
    existing = _existing_command(session, ctx, semantic_key_hash)
    if existing is not None:
        published = publication_mod.existing_publication(session, ctx, output.id)
        binding_id = ""
        if published is not None:
            bound = session.scalar(
                select(MarketAssetBindingRecord).where(
                    MarketAssetBindingRecord.workspace_id == ctx.workspace_id,
                    MarketAssetBindingRecord.publication_id == published.id,
                )
            )
            binding_id = bound.id if bound is not None else ""
        return AdoptIsolatedOutputResult(
            item_id=item_id,
            attempt_id=attempt_id,
            replayed=True,
            asset_version_id=published.asset_version_id if published else "",
            publication_id=published.id if published else "",
            binding_id=binding_id,
        )

    if item.state == "succeeded":
        # Succeeded through some *other* path (a normal finalize, or an
        # earlier Attempt's adoption). Publishing a second output for the
        # same Item would supersede a binding the user never asked to
        # replace, so this is refused rather than replayed.
        raise BatchValidationError(("already_adopted",))

    # Every authoritative recheck, repeated here exactly as
    # `handle_finalize_item` repeats them before it publishes.
    findings: list[str] = []
    report = validate_source_for_batch(
        session, ctx, item.source_asset_version_id, item.decision_id,
        requested_evidence_tier=_PROVIDER_TO_EXPERIMENT_MODE.get(batch.provider_execution_mode, "mock"),
    )
    if not report.valid:
        findings.extend(f"source:{finding}" for finding in report.findings)
    freshness = assess_truth_freshness(session, ctx, batch.truth_package_id, at=now)
    if not freshness.is_current:
        findings.extend(f"truth_stale:{reason}" for reason in freshness.reasons)
    copy_version = session.get(LocalizedCopyVersionRecord, item.copy_version_id)
    if copy_version is None or copy_version.state != "approved":
        findings.append("copy_not_approved")

    deterministic = qc_mod.derive_qc_eligibility(
        session, ctx, attempt_id=attempt_id, item_id=item_id, batch_id=batch_id
    )
    if deterministic.missing_slot_keys:
        findings.append("qc_plan_incomplete")
        findings.extend(
            f"qc_missing:{slot}" for slot in deterministic.missing_slot_keys
        )
    if deterministic.blocking_failure_slot_keys:
        findings.extend(
            f"qc_failed:{slot}" for slot in deterministic.blocking_failure_slot_keys
        )

    missing_external = qc_mod.missing_external_qc_steps(
        session, ctx, batch=batch, attempt_id=attempt_id
    )
    if missing_external:
        findings.append("qc_plan_incomplete")
        findings.extend(f"external_qc_missing:{step}" for step in missing_external)

    if findings:
        # De-duplicated, order preserved: `qc_plan_incomplete` is raised by
        # both the deterministic and the external gap and the caller should
        # see it once.
        raise BatchValidationError(tuple(dict.fromkeys(findings)))

    # The proof that the rechecks above actually ran, bound to what they
    # saw. `ck_lop_recheck_hash` requires it for an adopted publication --
    # the schema will not accept an adoption that cannot show one.
    recheck_hash = _hash(
        {
            "cmd": "adopt_isolated_output",
            "attempt_id": attempt_id,
            "item_id": item_id,
            "batch_id": batch_id,
            "output_id": output.id,
            "source_valid": True,
            "truth_freshness": "current",
            "copy_version_id": item.copy_version_id,
            "deterministic_slots": list(deterministic.frozen_slot_keys),
            "external_steps": list(qc_mod.frozen_external_qc_steps(batch)),
        }
    )

    published = publication_mod.publish_localized_output(
        session,
        ctx,
        batch=batch,
        item=item,
        attempt=attempt,
        output=output,
        publication_kind="adopted",
        actor_id=actor_id,
        role=role,
        recheck_hash=recheck_hash,
        now=now,
    )

    item.state = "succeeded"
    item.reason_code = "adopted_isolated_output"
    item.reason_detail = f"adopted by {actor_id} ({role})"
    item.recoverable = None
    item.next_action = None
    item.revision += 1
    item.updated_at = now

    _record_command(
        session, ctx, batch_id=batch_id, item_id=item_id, command_kind="adopt_isolated_output",
        operation_id=None, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=request_fingerprint_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return AdoptIsolatedOutputResult(
        item_id=item_id,
        attempt_id=attempt_id,
        replayed=False,
        asset_version_id=published.asset_version_id,
        publication_id=published.publication_id,
        binding_id=published.binding_id,
    )


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def cancel(
    session: Session,
    ctx: TenantContext,
    *,
    batch_id: str,
    expected_revision: int,
    actor_id: str,
    now: datetime | None = None,
) -> CancelResult:
    """`localization.cancel` (spec §2E/§2H): "Cancel unstarted Jobs and
    release reservations" -- no new Operation. Every non-terminal Item
    moves to `cancelled`; an already-dispatched, indivisible call may
    still complete and leave its output isolated for later audit/adoption
    (spec §2H) -- this command does not touch already-terminal Items or
    already-persisted AttemptOutputs.

    The Batch's `phase` transition is a real CAS `UPDATE ... WHERE
    revision = expected_revision` (spec §3F). Per-Item cancellation stays
    a plain write -- Items are not independently retried against this
    command the way `retry_item`/`resume_item`'s target Item is.
    """
    now = now or datetime.now(timezone.utc)
    batch = _load_batch(session, ctx, batch_id)

    if batch.phase in _TERMINAL_BATCH_PHASES:
        return CancelResult(batch_id=batch_id, cancelled_item_ids=(), already_terminal=True)

    items = session.scalars(
        select(LocalizationItemRecord).where(
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.batch_id == batch_id,
        )
    ).all()

    cancelled_ids: list[str] = []
    for item in items:
        if item.state in _ACTIVE_ITEM_STATES:
            item.state = "cancelled"
            item.reason_code = "batch_cancelled"
            item.reason_detail = f"batch cancelled by {actor_id}"
            item.recoverable = False
            item.next_action = None
            item.revision += 1
            item.updated_at = now
            cancelled_ids.append(item.id)

    cas_result = session.execute(
        update(LocalizationBatchRecord)
        .where(
            LocalizationBatchRecord.id == batch_id,
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.revision == expected_revision,
        )
        .values(phase="cancelled", revision=expected_revision + 1, updated_at=now)
    )
    if cas_result.rowcount != 1:
        raise StaleRevisionError("batch", batch_id, expected_revision)
    session.flush()
    session.refresh(batch)

    semantic_key_hash = _hash({"cmd": "cancel", "batch_id": batch_id, "at": now.isoformat()})
    _record_command(
        session, ctx, batch_id=batch_id, item_id=None, command_kind="cancel",
        operation_id=None, semantic_key_hash=semantic_key_hash,
        request_fingerprint_hash=semantic_key_hash, actor_id=actor_id, now=now,
    )
    session.flush()

    return CancelResult(
        batch_id=batch_id, cancelled_item_ids=tuple(cancelled_ids), already_terminal=False,
    )


# ---------------------------------------------------------------------------
# derive_batch_outcome
# ---------------------------------------------------------------------------


def derive_batch_outcome(
    session: Session,
    ctx: TenantContext,
    batch_id: str,
    *,
    expected_revision: int,
    now: datetime | None = None,
) -> str:
    """Spec §2F: runs only after every Item is terminal.

    - `failed` when no market was executable (every Item was
      `skipped_market_unavailable`), or every executable Item failed/cancelled;
    - `partially_succeeded` when a successful executable Item has any failed,
      cancelled, or unavailable sibling;
    - `succeeded` only when every Item succeeded or used
      `skipped_no_change_required`.

    The Batch's `outcome`/`phase` write is a real CAS
    `UPDATE ... WHERE revision = expected_revision` (spec §3F).
    """
    now = now or datetime.now(timezone.utc)
    batch = _load_batch(session, ctx, batch_id)

    items = session.scalars(
        select(LocalizationItemRecord).where(
            LocalizationItemRecord.workspace_id == ctx.workspace_id,
            LocalizationItemRecord.batch_id == batch_id,
        )
    ).all()
    if not items:
        raise BatchValidationError(("no_items",))

    non_terminal = [i for i in items if i.state not in _TERMINAL_ITEM_STATES]
    if non_terminal:
        raise BatchNotTerminalError(tuple(i.id for i in non_terminal))

    executable = [
        i for i in items
        if not (i.state == "skipped" and i.reason_code == "skipped_market_unavailable")
    ]
    unavailable_count = len(items) - len(executable)
    if not executable:
        outcome = "failed"
    else:
        good = [
            i for i in executable
            if i.state == "succeeded"
            or (i.state == "skipped" and i.reason_code == "skipped_no_change_required")
        ]
        if len(good) == len(executable):
            outcome = (
                "partially_succeeded" if unavailable_count else "succeeded"
            )
        elif not good:
            outcome = "failed"
        else:
            outcome = "partially_succeeded"

    cas_result = session.execute(
        update(LocalizationBatchRecord)
        .where(
            LocalizationBatchRecord.id == batch_id,
            LocalizationBatchRecord.workspace_id == ctx.workspace_id,
            LocalizationBatchRecord.revision == expected_revision,
        )
        .values(outcome=outcome, phase="closed", revision=expected_revision + 1, updated_at=now)
    )
    if cas_result.rowcount != 1:
        raise StaleRevisionError("batch", batch_id, expected_revision)
    session.flush()

    return outcome
