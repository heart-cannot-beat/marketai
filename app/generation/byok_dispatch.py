"""Worker dispatch of a ``byok`` RunItem against a real provider.

The mock paths in ``app.generation.worker`` never leave the process. A
``byok`` item does: it spends the Workspace's own money with the
Workspace's own key, so it goes through the three-phase
``ExternalCallCoordinator`` (``app.providers.dispatch``) exactly like the
``controlled_mock`` seam does -- committed prepare, network call outside any
transaction, committed completion -- with the pieces a real call adds:

1. the bound credential, opened from its envelope for the duration of the
   call and handed to the adapter as an ephemeral ``memoryview``;
2. the item's open ``SpendReservation`` (reserved by the command that
   admitted the item -- ``app.generation.spend``), which the coordinator
   settles on success and releases on a terminal failure;
3. the RunItem's provider-neutral :class:`CreativeRecipe` (built from the
   Run's frozen recipe snapshot, ``app.generation.creative_recipe``), which
   the adapter translates itself -- its ``plan`` (final prompt and the
   reference images it sends) is what the AssetVersion records, so the
   audited prompt and the reference lineage are exactly what was sent;
4. a Workspace-bound :class:`ReferenceReader`, through which the adapter
   reads the recipe's committed reference derivatives at call time;
5. a blob-committing evidence recorder, so the produced image lands in the
   bucket under a committed key the gallery can serve.

Outcomes map onto the RunItem the way the spec wants them: a terminal
provider refusal (or a billed answer without an image) fails the item with
the provider's reason, an unknown outcome (timeout, 5xx, storage failure
after the call) parks it as ``uncertain`` for reconciliation, and a missing
credential pauses it (``credential_unavailable`` is recoverable: bind a key
and Resume).

Every item also carries the reservation of its quality check (Issue #169,
``app.generation.vlm_qc``), made by the same admitting command. When the
image call itself ends the item (refused before dispatch, or a terminal
provider failure) no image will ever be checked, so that reservation is
released here; an ``uncertain`` item keeps it for reconciliation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, ContextManager, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.models import GenerationRunRecord
from app.entitlements.credential_protector import CredentialProtector
from app.generation.creative_recipe import (
    build_creative_recipe,
    committed_key_in_workspace,
    recipe_lineage,
)
from app.generation.credential_binding import open_bound_secret, resolve_generation_credential
from app.generation.evidence import GenerationEvidenceRecorder
from app.generation.execution_policy import StepRejection
from app.generation.image_call import (
    ByokGenerationRequest,
    ProviderCallPlan,
    ReferenceReader,
    plan_for,
)
from app.generation.models import RunItemRecord
from app.generation.spend import open_reservation_for_item
from app.generation.worker import (
    _DISPATCHABLE_STATES,
    _fail_or_pause_item,
    _generation_semantic_key_hash,
    _get_job_id,
    _get_operation_id,
    _sync_run_state_best_effort,
    _transition_item,
)
from app.platform.blob_store import BlobStore
from app.platform.tenancy import TenantContext
from app.providers.adapter import ExternalCallAdapter
from app.providers.dispatch import CoordinatedCallResult, ExternalCallCoordinator
from app.platform.runtime_role import WORKER_ROLE, runs_as

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedCall:
    item_id: str
    run_id: str
    job_id: str
    operation_id: str
    call_semantic_key_hash: str
    reservation_id: str
    credential_version_id: str
    provider: str
    model: str
    request: ByokGenerationRequest
    plan: ProviderCallPlan
    secret: bytes


@runs_as(WORKER_ROLE)
async def handle_run_item_byok(
    coordinator: ExternalCallCoordinator,
    ctx: TenantContext,
    run_item_id: str,
    *,
    adapters: Mapping[str, ExternalCallAdapter],
    blob_store: BlobStore,
    protector: CredentialProtector,
    session_factory: Callable[[TenantContext], ContextManager[Session]],
) -> CoordinatedCallResult | None:
    """Dispatch one ``byok`` RunItem; ``None`` when there was nothing to dispatch."""
    with session_factory(ctx) as session:
        prepared = _prepare_call(
            session, ctx, run_item_id,
            adapters=adapters, protector=protector, blob_store=blob_store,
        )
    if prepared is None:
        return None

    adapter = adapters[prepared.provider]
    recorder = GenerationEvidenceRecorder(
        prepared.item_id,
        blob_store=blob_store,
        credential_version_id=prepared.credential_version_id,
        prompt=prepared.plan.prompt,
        reference_sources=recipe_lineage(prepared.request.recipe, prepared.plan.references),
    )
    secret = bytearray(prepared.secret)
    try:
        result = await coordinator.execute(
            ctx,
            job_id=prepared.job_id,
            operation_id=prepared.operation_id,
            call_semantic_key_hash=prepared.call_semantic_key_hash,
            step_kind="generation",
            execution_mode="byok",
            chargeability="charged",
            provider=prepared.provider,
            request=prepared.request,
            adapter=adapter,
            evidence_recorder=recorder,
            credential=memoryview(secret),
            spend_reservation_id=prepared.reservation_id,
            run_item_id=prepared.item_id,
            model=prepared.model,
            credential_version_id=prepared.credential_version_id,
        )
    finally:
        for index in range(len(secret)):
            secret[index] = 0

    if not result.success:
        with session_factory(ctx) as session:
            _project_failure(session, ctx, prepared.item_id, result)
    return result


def _prepare_call(
    session: Session,
    ctx: TenantContext,
    run_item_id: str,
    *,
    adapters: Mapping[str, ExternalCallAdapter],
    protector: CredentialProtector,
    blob_store: BlobStore | None = None,
) -> _PreparedCall | None:
    item = session.execute(
        select(RunItemRecord).where(
            RunItemRecord.id == run_item_id,
            RunItemRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()
    if item.state not in _DISPATCHABLE_STATES:
        return None

    run = session.execute(
        select(GenerationRunRecord).where(
            GenerationRunRecord.id == item.generation_run_id,
            GenerationRunRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()

    if run.provider not in adapters:
        _release_quality_check(session, ctx, item)
        _fail_or_pause_item(session, ctx, item, StepRejection(
            code="mode_unavailable",
            detail=f"no BYOK adapter is deployed for provider {run.provider!r}",
            recoverable=False,
        ))
        return None

    evidence = resolve_generation_credential(session, ctx, run.provider)
    if not evidence.get("active"):
        # Resume enqueues a fresh Job that reserves its own check again.
        _release_quality_check(session, ctx, item)
        _fail_or_pause_item(session, ctx, item, StepRejection(
            code="credential_unavailable",
            detail=f"no active {run.provider} credential is bound for generation",
            recoverable=True,
        ))
        return None

    job_id = _get_job_id(session, ctx, item)
    reservation = open_reservation_for_item(session, ctx, item, job_id)
    if reservation is None:
        _release_quality_check(session, ctx, item)
        _fail_or_pause_item(session, ctx, item, StepRejection(
            code="reservation_missing",
            detail="no open spend reservation for this item; retry it to reserve again",
            recoverable=False,
        ))
        return None

    recipe = build_creative_recipe(session, ctx, item, run)
    if any(
        not committed_key_in_workspace(reference.committed_key, ctx.workspace_id)
        for reference in recipe.references_to_send
    ):
        # Defence in depth: freeze-time reads are Workspace-scoped, so this
        # only trips on a tampered snapshot. Nothing is sent.
        _release_quality_check(session, ctx, item)
        _fail_or_pause_item(session, ctx, item, StepRejection(
            code="reference_not_in_workspace",
            detail="a reference image of this recipe is not an object of this Workspace",
            recoverable=False,
        ))
        return None
    request = ByokGenerationRequest(
        recipe=recipe,
        model=run.model,
        semantic_fingerprint=item.semantic_fingerprint,
        reference_reader=ReferenceReader(ctx.workspace_id, blob_store),
    )
    plan = plan_for(adapters[run.provider], request)

    secret = open_bound_secret(session, ctx, evidence["version_id"], protector)
    return _PreparedCall(
        item_id=item.id,
        run_id=run.id,
        job_id=job_id,
        operation_id=_get_operation_id(session, ctx, item),
        call_semantic_key_hash=_generation_semantic_key_hash(item.semantic_fingerprint),
        reservation_id=reservation.id,
        credential_version_id=evidence["version_id"],
        provider=run.provider,
        model=run.model,
        request=request,
        plan=plan,
        secret=secret,
    )


def _release_quality_check(session: Session, ctx: TenantContext, item: RunItemRecord) -> None:
    """Release the quality-check reservation of an item that produced no image."""
    from app.generation.vlm_qc import QC_CALL_NAME
    from app.providers.llm_chat import release_llm_chat_reservation

    try:
        job_id = _get_job_id(session, ctx, item)
    except ValueError:
        return
    release_llm_chat_reservation(session, ctx, job_id=job_id, call_name=QC_CALL_NAME)


def _project_failure(
    session: Session, ctx: TenantContext, item_id: str, result: CoordinatedCallResult,
) -> None:
    """Reflect a non-success dispatch on the RunItem (terminal or uncertain)."""
    item = session.execute(
        select(RunItemRecord).where(
            RunItemRecord.id == item_id,
            RunItemRecord.workspace_id == ctx.workspace_id,
        )
    ).scalar_one()
    if item.state not in ("running", *_DISPATCHABLE_STATES):
        return
    summary = f"{result.error_code or 'provider_error'}: {result.error_detail or ''}"[:1000]
    # ``dispatched`` failures were answered (and billed) without a usable
    # image: terminal like a refusal, not a reconciliation case.
    state = "failed" if result.dispatch_certainty in ("not_dispatched", "dispatched") else "uncertain"
    if state == "failed":
        _release_quality_check(session, ctx, item)
    _transition_item(session, ctx, item, state=state, error_summary=summary)
    _sync_run_state_best_effort(session, ctx, item.generation_run_id)
    logger.warning(
        "generation_run_item_byok_%s",
        state,
        extra={
            "workspace_id": ctx.workspace_id,
            "run_item_id": item_id,
            "attempt_id": result.attempt_id,
            "error_code": result.error_code,
        },
    )
