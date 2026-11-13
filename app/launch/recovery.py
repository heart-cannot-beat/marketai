"""Operation recovery and reconciliation for uncertain Launch operations
(spec §8.3-§8.4).

An Operation lands in ``uncertain`` when a Meta call's dispatch marker
(``MetaCallIntentRecord.network_dispatched_at``) was persisted but the
process never recorded the matching ``MetaCallOutcomeRecord`` -- a crash, a
timeout, or any other gap between "the request left" and "the response was
recorded." The intent itself does not say whether the write reached the
sandbox backend.

Recovery never re-executes the write. Every write in this codebase (the
Sandbox-only ``FakeMetaAdapter``) produces a deterministic remote object id
from ``(scenario_version_id, object_type, idempotency_key)`` specifically so
recovery can recompute that id -- a pure hash, not a network or database
write -- and ask the adapter to *read* it back
(:meth:`~app.adapters.meta.fake_adapter.FakeMetaAdapter.read_object`). Finding
the object confirms the write happened; not finding it confirms it did not.
Either way, nothing is created a second time.

Two records carry this: :class:`~app.launch.models.LaunchReconciliationRecord`
serializes recovery at the Operation level (at most one open reconciliation
per original operation -- spec §8.4), and
:class:`~app.meta.models.MetaCallReconciliationRecord` records, per uncertain
intent, the read-back inspection call that resolved it.
"""
from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..adapters.meta.fake_adapter import FakeMetaAdapter
from ..adapters.persistence.models import new_id, utcnow
from ..meta.credential_resolution import create_call_intent
from ..meta.models import (
    MetaCallIntentRecord,
    MetaCallOutcomeRecord,
    MetaCallReconciliationRecord,
    MetaConnectionRecord,
    MetaDataSourceBindingRecord,
)
from ..platform.operations import (
    OperationState,
    get_operation,
    create_operation,
    transition_operation,
)
from ..platform.tenancy import TenantContext, tenant_scope
from .models import (
    LaunchCandidateRecord,
    LaunchParityCheckRecord,
    LaunchProvisioningRecord,
    LaunchProvisioningStatusHistoryRecord,
    LaunchReconciliationRecord,
    LaunchSagaStepAttemptRecord,
    MetaLaunchPreviewRecord,
)
from ..platform.runtime_role import WORKER_ROLE, runs_as

__all__ = [
    "RecoveryError",
    "detect_uncertain_intents",
    "create_recovery_operation",
    "execute_recovery",
]


class RecoveryError(RuntimeError):
    """Raised when a recovery request cannot be admitted or resolved."""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_uncertain_intents(
    session: Session, operation_id: str
) -> list[MetaCallIntentRecord]:
    """Intents dispatched to Meta with no recorded outcome yet.

    ``network_dispatched_at IS NOT NULL`` is the dispatch marker (spec
    §5.3/§8.3): the point past which "no outcome" no longer means "never
    happened," it means "unknown." Ordered by creation so callers see the
    saga's original call order.
    """
    stmt = (
        select(MetaCallIntentRecord)
        .where(
            MetaCallIntentRecord.operation_id == operation_id,
            MetaCallIntentRecord.network_dispatched_at.isnot(None),
            ~exists().where(
                MetaCallOutcomeRecord.intent_id == MetaCallIntentRecord.id
            ),
        )
        .order_by(MetaCallIntentRecord.created_at)
    )
    return list(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Recovery admission (spec §8.4: at most one open reconciliation per
# original operation)
# ---------------------------------------------------------------------------

def create_recovery_operation(
    session: Session,
    *,
    context: TenantContext,
    original_operation_id: str,
    recovery_operation=None,
) -> LaunchReconciliationRecord:
    """Open a recovery Operation + ``LaunchReconciliationRecord`` for ``original_operation_id``.

    Only an ``uncertain`` Operation may be recovered, and only one
    reconciliation may be open per original Operation at a time -- enforced
    here (a pre-check, for a clear error) and again at the database via
    ``uq_lrec_open_original_operation`` (a partial unique index on
    ``original_operation_id`` where ``state = 'open'``), which is the actual
    authority under concurrent recovery attempts.
    """
    if context.meta_mode is None:
        raise RecoveryError(
            "create_recovery_operation requires a TenantContext with meta_mode set"
        )

    with tenant_scope(session, context):
        original = get_operation(session, original_operation_id)
        if original.state != OperationState.UNCERTAIN.value:
            raise RecoveryError(
                f"operation {original_operation_id!r} is {original.state!r}, "
                "not 'uncertain'; recovery only applies to uncertain operations"
            )

        existing_open = session.execute(
            select(LaunchReconciliationRecord.id).where(
                LaunchReconciliationRecord.workspace_id == context.workspace_id,
                LaunchReconciliationRecord.original_operation_id == original_operation_id,
                LaunchReconciliationRecord.state == "open",
            )
        ).scalar_one_or_none()
        if existing_open is not None:
            raise RecoveryError(
                f"a recovery reconciliation is already open for operation "
                f"{original_operation_id!r}"
            )

        recovery_operation = recovery_operation or create_operation(
            session, command_type="launch_recovery", actor=context.executor,
        )

        reconciliation = LaunchReconciliationRecord(
            id=new_id("lrec"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            original_operation_id=original_operation_id,
            recovery_operation_id=recovery_operation.id,
            state="open",
        )
        try:
            with session.begin_nested():
                session.add(reconciliation)
                session.flush()
        except IntegrityError as exc:
            # Backstop for a concurrent recovery request racing the
            # pre-check above -- the partial unique index is the real
            # authority; this only turns its rejection into RecoveryError.
            raise RecoveryError(
                f"a recovery reconciliation is already open for operation "
                f"{original_operation_id!r}"
            ) from exc

        return reconciliation


# ---------------------------------------------------------------------------
# Recovery execution: read back adapter state, never re-execute writes
# ---------------------------------------------------------------------------

@runs_as(WORKER_ROLE)
def execute_recovery(
    session: Session,
    context: TenantContext,
    reconciliation_id: str,
) -> LaunchReconciliationRecord:
    """Resolve an open reconciliation by reading back adapter state.

    For each uncertain intent this recomputes the deterministic remote
    object id the original write would have produced and reads it back
    (:meth:`FakeMetaAdapter.read_object`) -- it never calls
    ``ensure_object`` again. If every uncertain intent can be resolved, the
    original Operation is transitioned out of ``uncertain`` (spec §5.2: the
    only transitions the state machine admits are ``uncertain -> succeeded
    / failed / running``, so a mixed result goes through ``running`` on its
    way to ``partially_succeeded`` -- the one backward edge the machine
    reserves for exactly this reconciler). If any intent cannot be resolved
    (no matching saga step, or the connection/binding it used is gone), the
    reconciliation is left ``open`` and the recovery Operation ends
    ``uncertain`` for manual follow-up.
    """
    with tenant_scope(session, context):
        reconciliation = session.execute(
            select(LaunchReconciliationRecord).where(
                LaunchReconciliationRecord.id == reconciliation_id,
                LaunchReconciliationRecord.workspace_id == context.workspace_id,
            )
        ).scalar_one_or_none()
        if reconciliation is None:
            raise RecoveryError(f"reconciliation {reconciliation_id!r} not found")
        if reconciliation.state != "open":
            raise RecoveryError(
                f"reconciliation {reconciliation_id!r} is already "
                f"{reconciliation.state!r}"
            )

        transition_operation(
            session, reconciliation.recovery_operation_id, OperationState.RUNNING,
            reason="recovery inspection started", actor=context.executor,
        )

        uncertain_intents = detect_uncertain_intents(
            session, reconciliation.original_operation_id
        )
        resolutions = [
            _inspect_uncertain_intent(session, context, reconciliation, intent)
            for intent in uncertain_intents
        ]

        if any(resolution == "inconclusive" for resolution in resolutions):
            transition_operation(
                session, reconciliation.recovery_operation_id, OperationState.UNCERTAIN,
                reason="recovery inspection inconclusive; manual review required",
                actor=context.executor,
            )
            session.flush()
            return reconciliation

        outcome, provisioning = _derive_manifest_outcome(
            session,
            context,
            original_operation_id=reconciliation.original_operation_id,
        )

        if outcome == "partially_succeeded":
            # The state machine has no direct uncertain -> partially_succeeded
            # edge; 'uncertain -> running' is the one backward transition it
            # reserves for a reconciler resuming an operation it has now
            # determined completed only in part (spec §5.2).
            transition_operation(
                session, reconciliation.original_operation_id, OperationState.RUNNING,
                reason="recovery resuming operation for partial resolution",
                actor=context.executor,
            )
            transition_operation(
                session, reconciliation.original_operation_id,
                OperationState.PARTIALLY_SUCCEEDED,
                reason="resolved partially by operation recovery", actor=context.executor,
                meta_mode=reconciliation.meta_mode,
            )
        else:
            to_state = (
                OperationState.SUCCEEDED if outcome == "succeeded" else OperationState.FAILED
            )
            transition_operation(
                session, reconciliation.original_operation_id, to_state,
                reason="resolved by operation recovery", actor=context.executor,
                meta_mode=reconciliation.meta_mode,
            )

        transition_operation(
            session, reconciliation.recovery_operation_id, OperationState.SUCCEEDED,
            reason="recovery inspection complete", actor=context.executor,
            meta_mode=reconciliation.meta_mode,
        )

        reconciliation.state = "resolved"
        reconciliation.resolution = outcome
        reconciliation.resolved_at = utcnow()
        target_provisioning_state = {
            "succeeded": "provisioned",
            "partially_succeeded": "partially_provisioned",
            "failed": "failed",
        }[outcome]
        if provisioning.provisioning_state != target_provisioning_state:
            previous_state = provisioning.provisioning_state
            provisioning.provisioning_state = target_provisioning_state
            provisioning.updated_at = utcnow()
            session.add(LaunchProvisioningStatusHistoryRecord(
                id=new_id("lpsh"),
                workspace_id=context.workspace_id,
                meta_mode=context.meta_mode,
                provisioning_id=provisioning.id,
                field="provisioning_state",
                from_state=previous_state,
                to_state=target_provisioning_state,
                reason="resolved by complete launch manifest reconciliation",
                executor=context.executor,
            ))
        session.flush()
        return reconciliation


def _reconstruct_parity_after_reconciliation(
    session: Session,
    context: TenantContext,
    *,
    provisioning: LaunchProvisioningRecord,
    adapter: FakeMetaAdapter,
    candidates: list[LaunchCandidateRecord],
    remote_campaign_id: str,
) -> LaunchParityCheckRecord:
    """Read-only parity reconstruction after complete reconciled manifest.

    Runs only once ``_derive_manifest_outcome`` has confirmed every expected
    write succeeded (directly or via reconciliation) and no ``post_provisioning``
    parity record exists yet -- the saga crashed or was interrupted before it
    could run its own parity check. Recovery reads adapter state -- via
    :meth:`~app.adapters.meta.fake_adapter.FakeMetaAdapter.read_object` and
    :meth:`~app.adapters.meta.fake_adapter.FakeMetaAdapter.deterministic_object_id`
    -- and never calls ``ensure_object()``, so nothing is created a second time.
    """
    campaign_state = adapter.read_object("campaign", remote_campaign_id)

    candidate_results = []
    for candidate in candidates:
        adset_id = adapter.deterministic_object_id("adset", candidate.adset_idempotency_key)
        ad_id = adapter.deterministic_object_id("ad", candidate.ad_idempotency_key)
        adset_state = adapter.read_object("adset", adset_id)
        ad_state = adapter.read_object("ad", ad_id)
        candidate_results.append({
            "candidate_id": candidate.id,
            "adset_remote_id": adset_id,
            "ad_remote_id": ad_id,
            "adset_state": adset_state,
            "ad_state": ad_state,
        })

    preview = session.get(MetaLaunchPreviewRecord, provisioning.preview_id)
    all_present = len(candidate_results) == len(candidates) == preview.candidate_count
    budget_ok = all(
        (r["adset_state"] or {}).get("daily_budget_minor") == preview.daily_budget_minor
        for r in candidate_results
    )
    config_ok = all(r["ad_state"] is not None for r in candidate_results)
    status_ok = (
        (campaign_state or {}).get("status") == "PAUSED"
        and all((r["adset_state"] or {}).get("status") == "PAUSED" for r in candidate_results)
        and all((r["ad_state"] or {}).get("status") == "PAUSED" for r in candidate_results)
    )
    passed = all_present and budget_ok and config_ok and status_ok

    parity_record = LaunchParityCheckRecord(
        id=new_id("lpc"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        provisioning_id=provisioning.id,
        check_type="post_provisioning",
        all_candidates_present=all_present,
        budget_parity_verified=budget_ok,
        config_parity_verified=config_ok,
        status_parity_verified=status_ok,
        detail={
            "source": "reconciliation",
            "remote_campaign_id": remote_campaign_id,
            "campaign_state": campaign_state,
            "candidates": candidate_results,
        },
        passed=passed,
        executor=context.executor,
    )
    session.add(parity_record)
    session.flush()
    return parity_record


def _derive_manifest_outcome(
    session: Session,
    context: TenantContext,
    *,
    original_operation_id: str,
) -> tuple[str, LaunchProvisioningRecord]:
    """Classify the whole expected write manifest, never only open intents."""
    provisioning = session.execute(
        select(LaunchProvisioningRecord).where(
            LaunchProvisioningRecord.workspace_id == context.workspace_id,
            LaunchProvisioningRecord.meta_mode == context.meta_mode,
            LaunchProvisioningRecord.operation_id == original_operation_id,
        ).with_for_update()
    ).scalar_one_or_none()
    if provisioning is None:
        raise RecoveryError(
            f"launch provisioning for operation {original_operation_id!r} not found"
        )
    candidates = list(session.scalars(
        select(LaunchCandidateRecord).where(
            LaunchCandidateRecord.workspace_id == context.workspace_id,
            LaunchCandidateRecord.meta_mode == context.meta_mode,
            LaunchCandidateRecord.preview_id == provisioning.preview_id,
        ).order_by(LaunchCandidateRecord.candidate_index)
    ).all())
    expected = {("create_campaign", None)}
    expected.update(("create_adset", candidate.id) for candidate in candidates)
    expected.update(("create_ad", candidate.id) for candidate in candidates)

    attempts = list(session.scalars(
        select(LaunchSagaStepAttemptRecord).where(
            LaunchSagaStepAttemptRecord.workspace_id == context.workspace_id,
            LaunchSagaStepAttemptRecord.meta_mode == context.meta_mode,
            LaunchSagaStepAttemptRecord.provisioning_id == provisioning.id,
            LaunchSagaStepAttemptRecord.step_type.in_([
                "create_campaign", "create_adset", "create_ad",
            ]),
        )
    ).all())
    reconciled = {
        row.uncertain_intent_id: row.resolution
        for row in session.scalars(
            select(MetaCallReconciliationRecord).join(
                MetaCallOutcomeRecord,
                MetaCallOutcomeRecord.id
                == MetaCallReconciliationRecord.inspection_outcome_id,
            ).where(
                MetaCallReconciliationRecord.workspace_id == context.workspace_id,
                MetaCallOutcomeRecord.inspected_operation_id == original_operation_id,
            )
        ).all()
    }
    succeeded: set[tuple[str, str | None]] = set()
    for attempt in attempts:
        key = (attempt.step_type, attempt.candidate_id)
        if key not in expected:
            continue
        has_outcome = session.scalar(
            select(exists().where(MetaCallOutcomeRecord.intent_id == attempt.intent_id))
        )
        if (attempt.state == "applied" and has_outcome) or (
            reconciled.get(attempt.intent_id) == "confirmed_succeeded"
        ):
            succeeded.add(key)

    parity_passed = session.scalar(
        select(exists().where(
            LaunchParityCheckRecord.workspace_id == context.workspace_id,
            LaunchParityCheckRecord.meta_mode == context.meta_mode,
            LaunchParityCheckRecord.provisioning_id == provisioning.id,
            LaunchParityCheckRecord.check_type == "post_provisioning",
            LaunchParityCheckRecord.passed.is_(True),
        ))
    )
    if expected and succeeded == expected and not parity_passed:
        # All writes succeeded but no parity record -- reconstruct read-only.
        # This happens when reconciliation confirmed all writes after
        # uncertainty and the saga never ran its own parity check.
        # Find the campaign attempt regardless of its recorded ``state``:
        # when the campaign write was the uncertain one, reconciliation
        # confirms it via ``reconciled.get(...) == "confirmed_succeeded"``
        # (see the loop above) while ``attempt.state`` stays
        # ``"intent_committed"``, never transitioning to ``"applied"``.
        # Any campaign attempt carries the intent we need, so filtering on
        # state here would (and did) raise StopIteration.
        campaign_step = next(
            (a for a in attempts if a.step_type == "create_campaign"),
            None,
        )
        if campaign_step is None:
            return "partially_succeeded", provisioning
        campaign_intent = session.get(
            MetaCallIntentRecord, campaign_step.intent_id
        )
        adapter = FakeMetaAdapter(
            session=session,
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            scenario_version_id=campaign_intent.scenario_version_id,
        )
        remote_campaign_id = adapter.deterministic_object_id(
            "campaign", provisioning.campaign_idempotency_key
        )
        parity = _reconstruct_parity_after_reconciliation(
            session, context,
            provisioning=provisioning,
            adapter=adapter,
            candidates=candidates,
            remote_campaign_id=remote_campaign_id,
        )
        if parity.passed:
            return "succeeded", provisioning
        return "partially_succeeded", provisioning

    if expected and succeeded == expected and parity_passed:
        return "succeeded", provisioning
    if not succeeded:
        return "failed", provisioning
    return "partially_succeeded", provisioning


def _inspect_uncertain_intent(
    session: Session,
    context: TenantContext,
    reconciliation: LaunchReconciliationRecord,
    intent: MetaCallIntentRecord,
) -> str:
    """Read back the object one uncertain intent would have written.

    Returns ``'confirmed_succeeded'``, ``'confirmed_failed'`` or
    ``'inconclusive'`` (no saga step, or missing connection/binding -- the
    intent cannot be safely resolved from adapter state alone).
    """
    attempt = session.execute(
        select(LaunchSagaStepAttemptRecord).where(
            LaunchSagaStepAttemptRecord.intent_id == intent.id,
        )
    ).scalar_one_or_none()
    if attempt is None or intent.scenario_version_id is None:
        return "inconclusive"

    connection = session.get(MetaConnectionRecord, intent.meta_connection_id)
    binding = session.get(MetaDataSourceBindingRecord, intent.meta_binding_id)
    if connection is None or binding is None:
        return "inconclusive"

    adapter = FakeMetaAdapter(
        session=session,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        scenario_version_id=intent.scenario_version_id,
    )

    object_type, remote_object_id = _expected_object(session, adapter, attempt, intent)
    if object_type is None or remote_object_id is None:
        return "inconclusive"

    found_state = adapter.read_object(object_type, remote_object_id)
    found = found_state is not None

    inspection_intent = create_call_intent(
        session,
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        connection=connection,
        binding=binding,
        operation_id=reconciliation.recovery_operation_id,
        meta_api_method="GET",
        meta_api_endpoint=f"/{object_type}s/{remote_object_id}",
        request_parameters={
            "object_type": object_type,
            "remote_object_id": remote_object_id,
            "recovery_of_intent_id": intent.id,
        },
        call_type="page_fetch",
        scenario_version_id=intent.scenario_version_id,
        executor=context.executor,
    )

    inspection_outcome = MetaCallOutcomeRecord(
        id=new_id("mco"),
        workspace_id=context.workspace_id,
        meta_mode=context.meta_mode,
        intent_id=inspection_intent.id,
        operation_id=reconciliation.recovery_operation_id,
        network_attempted=True,
        http_status=200 if found else 404,
        response_summary={"found": found, "remote_object_id": remote_object_id},
        inspected_operation_id=reconciliation.original_operation_id,
        executor=context.executor,
    )
    session.add(inspection_outcome)
    session.flush()

    resolution = "confirmed_succeeded" if found else "confirmed_failed"
    session.add(
        MetaCallReconciliationRecord(
            id=new_id("mcr"),
            workspace_id=context.workspace_id,
            meta_mode=context.meta_mode,
            uncertain_intent_id=intent.id,
            inspection_intent_id=inspection_intent.id,
            inspection_outcome_id=inspection_outcome.id,
            resolution=resolution,
        )
    )
    session.flush()
    return resolution


#: Saga step type -> (object type, idempotency key field owner).
_CREATE_STEP_OBJECT_TYPES = {
    "create_campaign": "campaign",
    "create_adset": "adset",
    "create_ad": "ad",
}


def _expected_object(
    session: Session,
    adapter: FakeMetaAdapter,
    attempt: LaunchSagaStepAttemptRecord,
    intent: MetaCallIntentRecord,
) -> tuple[str | None, str | None]:
    """The ``(object_type, remote_object_id)`` this saga step would have written or read.

    Readback steps already carry ``remote_object_id`` in the intent's
    request parameters -- no recomputation needed. Create steps carry no
    remote id (the whole point of "uncertain" is that it was never
    recorded), so the id is recomputed the same way ``FakeMetaAdapter``
    would have produced it: deterministically, from the idempotency key the
    Launch domain already persisted on the provisioning/candidate record.
    """
    step_type = attempt.step_type
    if step_type.startswith("readback_"):
        params = intent.request_parameters or {}
        return params.get("object_type"), params.get("remote_object_id")

    object_type = _CREATE_STEP_OBJECT_TYPES.get(step_type)
    if object_type is None:
        return None, None

    if object_type == "campaign":
        prov = session.execute(
            select(LaunchProvisioningRecord).where(
                LaunchProvisioningRecord.id == attempt.provisioning_id,
            )
        ).scalar_one_or_none()
        if prov is None:
            return None, None
        idempotency_key = prov.campaign_idempotency_key
    else:
        if attempt.candidate_id is None:
            return None, None
        candidate = session.execute(
            select(LaunchCandidateRecord).where(
                LaunchCandidateRecord.id == attempt.candidate_id,
            )
        ).scalar_one_or_none()
        if candidate is None:
            return None, None
        idempotency_key = (
            candidate.adset_idempotency_key
            if object_type == "adset"
            else candidate.ad_idempotency_key
        )

    return object_type, adapter.deterministic_object_id(object_type, idempotency_key)
