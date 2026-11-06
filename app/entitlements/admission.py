"""PaidOperationAdmission: 10-step entitlement gate for paid operations.

Spec section 3.2.  The ``admit_in_transaction`` method runs entirely within
the caller's database transaction.  It validates entitlements, claims a
semantic intent, creates an OperationRecord + JobRecords, generates quotes,
builds a ReservationPlan, and reserves spend/capacity atomically.

Idempotency:  If an OperationRecord with the same ``semantic_key_hash``
already exists for the workspace, the call is a replay and the method returns
a reconstructed ``AdmissionResult`` with ``replayed=True`` without creating
duplicate records or reservations.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.adapters.persistence.models import (
    JobRecord,
    OperationRecord,
)
from app.entitlements.cost_models import (
    CapacityReservationRecord,
    SpendReservationRecord,
)
from app.entitlements.policy import EntitlementPolicy
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import (
    ReservationBook,
    capacity_reservation_key,
)
from app.entitlements.types import (
    AdmissionRequest,
    AdmissionResult,
    EntitlementRef,
    ReservationBundle,
    ReservationPlan,
)
from app.platform.operations import (
    OperationResultRef,
    OperationState,
    create_operation,
)
from app.platform.tenancy import TenantContext


ADMISSION_CONTRACT_VERSION = "admission-v1"
EXECUTION_CONTRACT_VERSION = 1


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SemanticIntentConflictError(Exception):
    """An OperationRecord with the same semantic_key_hash already exists
    but with a different request_fingerprint_hash."""

    def __init__(self, existing_operation_id: str, semantic_key_hash: str) -> None:
        self.existing_operation_id = existing_operation_id
        self.semantic_key_hash = semantic_key_hash
        super().__init__(
            f"semantic intent conflict: operation {existing_operation_id} "
            f"already holds semantic_key_hash {semantic_key_hash}"
        )


class InvalidProviderModeError(Exception):
    """The provider selection's execution mode is not valid for the workspace."""

    def __init__(self, mode: str, reason: str) -> None:
        self.mode = mode
        self.reason = reason
        super().__init__(f"invalid provider mode {mode!r}: {reason}")


class IncompleteAdmissionEvidenceError(Exception):
    """A legacy pending Job cannot be proven safe to publish.

    ``pending`` was historically committed by the admission path even though
    the queue only claims ``queued``/``retry_wait``.  Recovery is intentionally
    scoped to an idempotent replay of the original semantic request.  If the
    durable Operation, Job, or reservation evidence does not match that
    request, the Job remains pending and dispatch fails closed.
    """

    def __init__(self, operation_id: str, reason: str) -> None:
        self.operation_id = operation_id
        self.reason = reason
        super().__init__(
            f"cannot publish pending jobs for operation {operation_id}: {reason}"
        )


class PaidOperationAdmission:
    """10-step admission gate that runs inside the caller's transaction."""

    def __init__(
        self,
        policy: EntitlementPolicy,
        quote_engine: CostQuoteEngine,
        reservation_book: ReservationBook,
    ) -> None:
        self._policy = policy
        self._quote_engine = quote_engine
        self._reservation_book = reservation_book

    def admit_in_transaction(
        self,
        session: Session,
        context: TenantContext,
        request: AdmissionRequest,
    ) -> AdmissionResult:
        """Execute the 10-step admission flow within the caller's transaction.

        Steps:
          1. Validate initiator role (implicit -- TenantContext is already verified).
          2. Authorize entitlement lease via policy.
          3. Claim semantic intent (check for existing OperationRecord).
          4. Handle replay -- return cached result if fingerprint matches.
          5. Validate provider execution mode.
          6. Create OperationRecord.
          7. Create JobRecords.
          8. Generate quotes via quote_engine.
          9. Build ReservationPlan.
         10. Reserve spend/capacity via reservation_book.
        """
        workspace_id = context.workspace_id
        now = _utcnow()

        # Step 1: Role validation -- TenantContext is already verified by middleware.
        # We trust the context here.

        # Step 2: Authorize entitlement lease via policy.
        entitlement_ref: EntitlementRef = self._policy.authorize(
            session, context, request.entitlement_requirements, now,
        )

        # Step 3: Claim semantic intent -- check for existing operation.
        existing_op = session.query(OperationRecord).filter_by(
            workspace_id=workspace_id,
            semantic_key_hash=request.semantic_key_hash,
        ).first()

        # Step 4: Handle replay.
        if existing_op is not None:
            if existing_op.request_fingerprint_hash != request.request_fingerprint_hash:
                raise SemanticIntentConflictError(
                    existing_op.id, request.semantic_key_hash,
                )
            return self._replay_result(
                session, existing_op, entitlement_ref, request,
            )

        # Step 5: Validate provider execution mode.
        # Currently all modes (BYOK, SANDBOX, MOCK) are accepted.
        # BYOK credential binding enforcement is deferred to the execution
        # layer -- Task 17 wires admission only; credential resolution
        # happens at job dispatch time.

        # Step 6: Create OperationRecord.
        #
        # Through ``create_operation`` so the record and its ``None ->
        # admitted`` history event are written in this same transaction
        # (spec §5.5). An OperationRecord without its creation event would
        # leave ``operation_records.state`` as a projection over nothing --
        # every later transition appends to a history that never recorded
        # how the Operation came to exist.
        operation_id = _new_id("op")
        create_operation(
            session,
            context=context,
            operation_id=operation_id,
            command_type=request.command_type,
            initial_state=OperationState.ADMITTED,
            reason="Operation admitted",
            semantic_key_hash=request.semantic_key_hash,
            request_fingerprint_hash=request.request_fingerprint_hash,
            result_ref=OperationResultRef(
                resource_type=request.operation_payload.result_resource_type,
                resource_id=request.operation_payload.result_resource_id,
            ),
            admission_contract_version=1,
            actor=context.initiator_id or context.executor,
            now=now,
        )

        # Step 7: Create JobRecords.
        job_ids_by_call_key: dict[str, str] = {}
        for job_spec in request.job_specs:
            job_id = _new_id("job")
            session.add(JobRecord(
                id=job_id,
                workspace_id=workspace_id,
                initiator_id=context.initiator_id,
                initiator_role=context.audit_initiator_role,
                type=job_spec.job_type,
                resource_type=job_spec.resource_type,
                resource_id=job_spec.resource_id,
                payload_json=job_spec.payload_json,
                idempotency_key=f"{operation_id}_{job_spec.call_semantic_key_hash}",
                state="pending",
                operation_id=operation_id,
                entitlement_version_at_enqueue=entitlement_ref.entitlement_version,
                execution_contract_version=EXECUTION_CONTRACT_VERSION,
                created_at=now,
            ))
            job_ids_by_call_key[job_spec.call_semantic_key_hash] = job_id

        session.flush()

        # Step 8: Generate quotes.
        budget_currency = session.query(
            OperationRecord.workspace_id,
        ).filter_by(id=operation_id).first()

        # Get workspace budget currency.
        from app.adapters.persistence.models import WorkspaceRecord
        ws = session.query(WorkspaceRecord).filter_by(id=workspace_id).first()
        budget_currency = ws.budget_currency if ws else "USD"

        quote_bundle = self._quote_engine.bundle(
            request.billable_calls,
            execution_mode=str(request.provider_selection.execution_mode),
            budget_currency=budget_currency,
        )

        # Step 9: Build ReservationPlan.
        plan = ReservationPlan(
            quote_bundle=quote_bundle,
            capacity_requests=request.capacity_requests,
            entitlement_ref=entitlement_ref,
            operation_id=operation_id,
            job_ids_by_call_key=job_ids_by_call_key,
        )

        # Step 10: Reserve spend/capacity via reservation_book.
        reservation_bundle: ReservationBundle = self._reservation_book.reserve(
            session, context, plan,
        )

        # Queue publication is the final admission step.  It deliberately
        # happens after ReservationBook has flushed all spend/capacity
        # evidence, but inside the same caller-owned transaction.  A failed
        # reservation therefore cannot leave a dispatchable Job behind.
        self._publish_jobs(
            session,
            session.query(JobRecord).filter_by(
                operation_id=operation_id,
                workspace_id=workspace_id,
            ).all(),
        )

        return AdmissionResult(
            operation_id=operation_id,
            entitlement_ref=entitlement_ref,
            reservation_bundle=reservation_bundle,
            replayed=False,
        )

    def _replay_result(
        self,
        session: Session,
        operation: OperationRecord,
        entitlement_ref: EntitlementRef,
        request: AdmissionRequest,
    ) -> AdmissionResult:
        """Reconstruct an AdmissionResult from an existing operation (replay)."""
        jobs = session.query(JobRecord).filter_by(
            operation_id=operation.id,
            workspace_id=operation.workspace_id,
        ).all()

        pending_jobs = [job for job in jobs if job.state == "pending"]
        if pending_jobs:
            self._verify_legacy_pending_evidence(
                session, operation, jobs, request,
            )
            self._publish_jobs(session, pending_jobs)

        job_ids = tuple(j.id for j in jobs)

        # Reconstruct a minimal ReservationBundle for replay.
        reservation_bundle = ReservationBundle(
            operation_id=operation.id,
            spend_reservation_by_call_key={},
            capacity_reservation_ids=(),
            job_ids=job_ids,
            guardrail_revision=0,
        )

        return AdmissionResult(
            operation_id=operation.id,
            entitlement_ref=entitlement_ref,
            reservation_bundle=reservation_bundle,
            replayed=True,
        )

    @staticmethod
    def _publish_jobs(session: Session, jobs: list[JobRecord]) -> None:
        """Publish admission-created Jobs after all evidence is durable."""
        for job in jobs:
            if job.state != "pending":
                continue
            job.execution_contract_version = EXECUTION_CONTRACT_VERSION
            job.state = "queued"
        session.flush()

    @staticmethod
    def _verify_legacy_pending_evidence(
        session: Session,
        operation: OperationRecord,
        jobs: list[JobRecord],
        request: AdmissionRequest,
    ) -> None:
        """Prove a historically committed ``pending`` admission is publishable.

        The caller has already matched the operation's semantic key and request
        fingerprint.  Here we additionally bind every persisted Job to the
        replayed JobSpec and require the reservation evidence implied by the
        replayed provider mode/capacity request.  This is deliberately not a
        generic pending-job sweeper.
        """
        def fail(reason: str) -> IncompleteAdmissionEvidenceError:
            return IncompleteAdmissionEvidenceError(operation.id, reason)

        if operation.state != OperationState.ADMITTED.value:
            raise fail(f"operation state is {operation.state!r}, not 'admitted'")
        if operation.admission_contract_version < 1:
            raise fail("operation has no durable admission contract")
        if len(jobs) != len(request.job_specs):
            raise fail("persisted Job count does not match the replayed request")

        jobs_by_call_key: dict[str, JobRecord] = {}
        for spec in request.job_specs:
            expected_key = f"{operation.id}_{spec.call_semantic_key_hash}"
            matches = [job for job in jobs if job.idempotency_key == expected_key]
            if len(matches) != 1:
                raise fail(
                    "missing or duplicate Job for call semantic key "
                    f"{spec.call_semantic_key_hash!r}"
                )
            job = matches[0]
            if (
                job.workspace_id != operation.workspace_id
                or job.operation_id != operation.id
                or job.entitlement_version_at_enqueue is None
                or job.type != spec.job_type
                or job.resource_type != spec.resource_type
                or job.resource_id != spec.resource_id
                or job.payload_json != spec.payload_json
            ):
                raise fail(
                    f"persisted Job {job.id!r} does not match its replayed JobSpec"
                )
            jobs_by_call_key[spec.call_semantic_key_hash] = job

        if request.provider_selection.execution_mode != "mock":
            for call in request.billable_calls:
                job = jobs_by_call_key.get(call.call_semantic_key_hash)
                if job is None:
                    raise fail(
                        "billable call has no matching Job: "
                        f"{call.call_semantic_key_hash!r}"
                    )
                reservation = session.query(SpendReservationRecord).filter_by(
                    workspace_id=operation.workspace_id,
                    operation_id=operation.id,
                    job_id=job.id,
                    semantic_key_hash=call.call_semantic_key_hash,
                ).one_or_none()
                if (
                    reservation is None
                    or reservation.state != "reserved"
                    or reservation.entitlement_version
                    != job.entitlement_version_at_enqueue
                ):
                    raise fail(
                        f"Job {job.id!r} has no matching spend reservation"
                    )

        admission_capacity = [
            req for req in request.capacity_requests
            if req.acquisition_phase == "reserve_on_admission"
        ]
        first_job = next(iter(jobs_by_call_key.values()), None)
        for capacity in admission_capacity:
            if first_job is None:
                raise fail("capacity reservation has no matching Job")
            reservation = session.query(CapacityReservationRecord).filter_by(
                workspace_id=operation.workspace_id,
                operation_id=operation.id,
                job_id=first_job.id,
                semantic_key_hash=capacity_reservation_key(
                    capacity.entitlement_key, operation.id
                ),
                entitlement_key=capacity.entitlement_key,
                quantity=int(capacity.quantity),
                unit=capacity.unit,
                acquisition_phase="admission",
            ).one_or_none()
            if (
                reservation is None
                or reservation.state != "reserved"
                or reservation.entitlement_version
                != first_job.entitlement_version_at_enqueue
            ):
                raise fail(
                    "missing capacity reservation for entitlement "
                    f"{capacity.entitlement_key!r}"
                )
