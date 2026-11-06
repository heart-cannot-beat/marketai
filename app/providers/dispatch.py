"""Generic, domain-agnostic external-call dispatch coordinator (spec §2D).

``ExternalCallCoordinator`` is the three-phase dispatch fence between an
admitted, reserved unit of external-call work and an actual provider API
call:

1. **prepare** (in a DB transaction) -- validate the Job/Operation exist and
   the chargeability/execution-mode/reservation relationship is consistent,
   create (or idempotently recover) the ``ProviderCallAttemptRecord`` header
   plus its initial CAS projection, and let the caller-supplied
   ``ExternalCallEvidenceRecorder`` link the attempt to its domain subject
   (a Localization work row, a Generation RunItem, ...) in the same
   transaction.
2. **dispatch** (outside any transaction) -- invoke the typed
   ``ExternalCallAdapter`` with an ephemeral credential view and the
   coordinator-issued idempotency key. The network call never holds a
   database lock.
3. **complete** (in a DB transaction) -- persist the outcome: append the
   append-only ProviderAttempt event(s), advance the CAS projection, settle
   or release the chargeable ``SpendReservation`` (appending a
   ``ProviderCostLedgerEntryRecord`` on settlement), and -- for a success --
   let the evidence recorder persist the durable typed result reference
   into domain evidence, all in the same transaction as the ProviderAttempt
   success event.

This module is intentionally domain-agnostic: ``step_kind`` is metadata
recorded on the attempt row, never a branch here, and this module never
imports a Localization or Generation ORM model. Callers select the adapter
and build the typed request; ``ExternalCallEvidenceRecorder`` is the only
seam back into caller-owned persistence, and the coordinator invokes it
without knowing what it does.

Five crash windows (spec §3G) are handled explicitly:

| # | Window                                          | Recovery                                    |
|---|--------------------------------------------------|----------------------------------------------|
| 1 | Job claimed, no attempt row yet                  | create the attempt idempotently on retry     |
| 2 | Attempt ``prepared``, network never started      | redispatch the same authorized attempt       |
| 3 | ``dispatching`` committed, no result evidence    | mark ``uncertain``; needs reconciliation     |
| 4 | ``succeeded``/``failed`` committed               | replay the stored outcome, never redispatch  |
| 5 | ``uncertain``, request maybe sent, no response   | capability-aware ``reconcile()``, never blind retry |

Usage-priced providers (``llm_chat``, Issue #160) attach a ``ReportedCost``
to their result -- or to a ``dispatched`` failure, when the provider billed
for an unusable answer. The completion phase then appends a
``usage_reported`` event and settles the reservation at that cost
(``reported_cost`` + ``release`` of the remainder) instead of at its maximum.

Windows 1/2/4 are handled transparently inside :meth:`execute`. Windows 3/5
never auto-retry from :meth:`execute`; an attempt found ``dispatching`` on
recovery is transitioned to ``uncertain`` and returned to the caller, who
must explicitly invoke :meth:`reconcile` (this is what "uncertain is
reconciled; it never creates a replacement call automatically" means).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, ContextManager, Generic, Protocol, TypeVar, runtime_checkable

from sqlalchemy.orm import Session

from app.adapters.persistence.models import JobRecord, OperationRecord, new_id
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.entitlements.reservation_book import ledger_entry_key
from app.platform.tenancy import TenantContext
from app.providers.adapter import ExternalCallAdapter, ProviderDispatchError, ReportedCost
from app.providers.attempts import append_event, prepare_attempt

RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: ``provider_cost_ledger`` amounts are ``Numeric(24, 12)``.
_LEDGER_QUANTUM = Decimal("0.000000000001")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DispatchCoordinatorError(Exception):
    """Base for coordinator-specific failures."""


class JobNotFoundError(DispatchCoordinatorError):
    """No Job exists for the given workspace + job_id."""


class OperationNotFoundError(DispatchCoordinatorError):
    """No Operation exists for the given workspace + operation_id."""


class ReservationInvalidError(DispatchCoordinatorError):
    """The spend reservation is missing or not in the expected state."""


class ChargeabilityMismatchError(DispatchCoordinatorError):
    """``chargeability``/``execution_mode``/``spend_reservation_id`` are
    inconsistent (mirrors ``ProviderCallAttemptRecord``'s relational CHECK,
    raised early with a clear message instead of a raw IntegrityError)."""


class ReconciliationNotApplicableError(DispatchCoordinatorError):
    """``reconcile()`` was called on an attempt that is not ``uncertain``."""


# ---------------------------------------------------------------------------
# Evidence recorder seam
# ---------------------------------------------------------------------------


@runtime_checkable
class ExternalCallEvidenceRecorder(Protocol[ResultT]):
    """The only seam the coordinator uses to reach caller-owned persistence.

    Three responsibilities, matching spec §2D exactly:

    * ``link_prepared_attempt`` -- in the *prepare* transaction, link the
      just-created ``ProviderCallAttemptRecord`` to its domain subject (a
      pre-existing work row for preparation calls; the existing Attempt for
      Item calls). Never creates a result row here -- it does not exist yet.
    * ``materialize`` -- outside any transaction, place a successful typed
      output into durable storage and return an opaque redacted
      reference/hash. An orphaned object from a later rollback is safe and
      garbage-collectable.
    * ``persist_result`` -- in the *complete* transaction, write that
      reference into domain evidence (creating an immutable result row and
      filling a work row's write-once result ID, when applicable).

    The coordinator calls all three without importing whatever ORM models
    the concrete implementation touches.
    """

    def link_prepared_attempt(
        self,
        session: Session,
        ctx: TenantContext,
        *,
        attempt: ProviderCallAttemptRecord,
    ) -> None: ...

    def materialize(
        self,
        ctx: TenantContext,
        *,
        attempt_id: str,
        output: ResultT,
    ) -> str: ...

    def persist_result(
        self,
        session: Session,
        ctx: TenantContext,
        *,
        attempt: ProviderCallAttemptRecord,
        reference: str,
        output: ResultT,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Result value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinatedCallResult(Generic[ResultT]):
    """Typed outcome of one coordinated dispatch cycle (fresh or replayed)."""

    attempt_id: str
    dispatch_certainty: str  # 'dispatched' | 'not_dispatched' | 'uncertain'
    success: bool
    replayed: bool
    provider_request_id: str | None
    evidence_reference: str | None
    error_code: str | None
    error_detail: str | None
    result: ResultT | None = None


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ExternalCallCoordinator:
    """Three-phase, domain-agnostic external-call dispatch coordinator.

    Constructor:
        session_factory -- callable(ctx) -> context-managed Session, commits
                            on clean exit and rolls back on exception
                            (same contract as ``app.entitlements.coordinator``).
    """

    def __init__(
        self,
        session_factory: Callable[[TenantContext], ContextManager[Session]],
    ) -> None:
        self._session_factory = session_factory

    # -- public entry points -------------------------------------------------

    async def execute(
        self,
        ctx: TenantContext,
        *,
        job_id: str,
        operation_id: str,
        call_semantic_key_hash: str,
        step_kind: str,
        execution_mode: str,
        chargeability: str,
        provider: str,
        request: RequestT,
        adapter: ExternalCallAdapter[RequestT, ResultT],
        evidence_recorder: ExternalCallEvidenceRecorder[ResultT],
        credential: memoryview | None = None,
        spend_reservation_id: str | None = None,
        truth_package_id: str | None = None,
        run_item_id: str | None = None,
        model: str | None = None,
        credential_version_id: str | None = None,
    ) -> CoordinatedCallResult[ResultT]:
        """Run (or idempotently resume) the full three-phase dispatch cycle.

        ``step_kind`` is persisted as metadata only -- it never selects the
        adapter or changes control flow here; the caller already chose
        ``adapter`` for this ``step_kind``. ``credential_version_id`` names
        the BYOK credential version ``credential`` was opened from, so the
        attempt and its cost-ledger entry record which key paid for the call.
        """
        prepared = self._prepare_in_transaction(
            ctx,
            job_id=job_id,
            operation_id=operation_id,
            call_semantic_key_hash=call_semantic_key_hash,
            step_kind=step_kind,
            execution_mode=execution_mode,
            chargeability=chargeability,
            provider=provider,
            evidence_recorder=evidence_recorder,
            spend_reservation_id=spend_reservation_id,
            truth_package_id=truth_package_id,
            run_item_id=run_item_id,
            model=model,
            credential_version_id=credential_version_id,
        )

        if prepared["replay"]:
            return CoordinatedCallResult(
                attempt_id=prepared["attempt_id"],
                dispatch_certainty=prepared["dispatch_certainty"],
                success=prepared["success"],
                replayed=True,
                provider_request_id=prepared["provider_request_id"],
                evidence_reference=prepared["evidence_reference"],
                error_code=prepared["error_code"],
                error_detail=prepared["error_detail"],
                result=None,
            )

        return await self._dispatch_and_complete(
            ctx,
            attempt_id=prepared["attempt_id"],
            idempotency_key=prepared["idempotency_key"],
            request=request,
            adapter=adapter,
            evidence_recorder=evidence_recorder,
            credential=credential,
        )

    async def reconcile(
        self,
        ctx: TenantContext,
        *,
        attempt_id: str,
        adapter: ExternalCallAdapter[RequestT, ResultT],
        evidence_recorder: ExternalCallEvidenceRecorder[ResultT],
        credential: memoryview | None = None,
        request: RequestT | None = None,
    ) -> CoordinatedCallResult[ResultT]:
        """Capability-aware reconciliation for an ``uncertain`` attempt
        (crash window 5). Never a blind retry:

        1. if a queryable provider request ID exists and the adapter
           supports lookup-by-request-id, look it up first;
        2. if the lookup returns a completed result, materialize and fold
           it (persist as a normal success, once);
        3. else, retry with the *same* idempotency key only when the
           adapter's capability guarantees idempotency for that key *and*
           the caller can supply the original typed ``request`` again
           (without it there is nothing safe to resend);
        4. else, remain ``uncertain`` for manual reconciliation.
        """
        with self._session_factory(ctx) as session:
            attempt = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(id=attempt_id, workspace_id=ctx.workspace_id)
                .one()
            )
            if attempt.dispatch_state != "uncertain":
                raise ReconciliationNotApplicableError(
                    f"attempt {attempt_id!r} is in dispatch_state "
                    f"{attempt.dispatch_state!r}, not 'uncertain'"
                )
            provider_request_id = attempt.provider_request_id
            idempotency_key = attempt.provider_idempotency_key or (
                f"{attempt.call_semantic_key_hash}:{attempt.attempt_no}"
            )

        capabilities = adapter.capabilities

        # Step 1-2: queryable lookup takes priority over any retry.
        if provider_request_id and capabilities.supports_result_lookup_by_request_id:
            lookup = getattr(adapter, "lookup_by_request_id", None)
            if lookup is not None:
                looked_up = await lookup(provider_request_id)
                if looked_up is not None:
                    evidence_reference = await self._materialize(
                        evidence_recorder, ctx, attempt_id=attempt_id, output=looked_up.output,
                    )
                    return self._complete_in_transaction(
                        ctx,
                        attempt_id=attempt_id,
                        evidence_recorder=evidence_recorder,
                        dispatch_result={
                            "success": True,
                            "provider_request_id": looked_up.provider_request_id,
                            "dispatch_certainty": "dispatched",
                            "error_code": None,
                            "error_detail": None,
                            "output": looked_up.output,
                        },
                        evidence_reference=evidence_reference,
                        reconciled=True,
                    )

        # Step 3: retry only when the provider contract guarantees
        # idempotency for the same key *and* the caller supplied the
        # original typed request to resend -- never a blind retry.
        if capabilities.accepts_idempotency_key and request is not None:
            return await self._dispatch_and_complete(
                ctx,
                attempt_id=attempt_id,
                idempotency_key=idempotency_key,
                request=request,
                adapter=adapter,
                evidence_recorder=evidence_recorder,
                credential=credential,
                reconciled=True,
            )

        # Step 4: inconclusive and not safely retryable -- stays uncertain.
        return CoordinatedCallResult(
            attempt_id=attempt_id,
            dispatch_certainty="uncertain",
            success=False,
            replayed=True,
            provider_request_id=provider_request_id,
            evidence_reference=None,
            error_code="reconciliation_inconclusive",
            error_detail=(
                "no queryable provider result and no idempotency guarantee; "
                "manual reconciliation required"
            ),
            result=None,
        )

    def find_uncertain_attempts(
        self,
        ctx: TenantContext,
        call_semantic_key_hash: str,
    ) -> list[dict[str, Any]]:
        """Find attempts stuck in ``uncertain`` state for SIGKILL recovery.

        After Worker restart, the caller queries by ``call_semantic_key_hash``
        to locate unfinished work, then feeds each result's ``attempt_id``
        into :meth:`reconcile`.
        """
        with self._session_factory(ctx) as session:
            attempts = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(
                    workspace_id=ctx.workspace_id,
                    call_semantic_key_hash=call_semantic_key_hash,
                    dispatch_state="uncertain",
                )
                .all()
            )
            return [
                {
                    "attempt_id": a.id,
                    "dispatch_state": a.dispatch_state,
                    "provider_idempotency_key": a.provider_idempotency_key,
                    "idempotency_key_hash": a.idempotency_key_hash,
                    "call_semantic_key_hash": a.call_semantic_key_hash,
                    "run_item_id": a.run_item_id,
                }
                for a in attempts
            ]

    # -- Phase 1: prepare ----------------------------------------------------

    def _prepare_in_transaction(
        self,
        ctx: TenantContext,
        *,
        job_id: str,
        operation_id: str,
        call_semantic_key_hash: str,
        step_kind: str,
        execution_mode: str,
        chargeability: str,
        provider: str,
        evidence_recorder: ExternalCallEvidenceRecorder[ResultT],
        spend_reservation_id: str | None,
        truth_package_id: str | None,
        run_item_id: str | None,
        model: str | None,
        credential_version_id: str | None = None,
    ) -> dict[str, Any]:
        with self._session_factory(ctx) as session:
            job = (
                session.query(JobRecord)
                .filter_by(id=job_id, workspace_id=ctx.workspace_id)
                .one_or_none()
            )
            if job is None:
                raise JobNotFoundError(f"no Job {job_id!r} in workspace {ctx.workspace_id!r}")

            operation = (
                session.query(OperationRecord)
                .filter_by(id=operation_id, workspace_id=ctx.workspace_id)
                .one_or_none()
            )
            if operation is None:
                raise OperationNotFoundError(
                    f"no Operation {operation_id!r} in workspace {ctx.workspace_id!r}"
                )

            # Look up any existing attempt for this exact job first: a
            # replay of an already-terminal (or in-flight) attempt must
            # never be blocked by the *current* reservation state -- by
            # the time a successful attempt is replayed the reservation
            # has already been settled (Important fix #1), and a failed
            # one already released. Chargeability/reservation validation
            # only applies to a genuinely fresh prepare, below.
            # One Job may make several distinct coordinated calls (an
            # ``llm_chat`` job asking two questions), so the attempt is
            # identified by the Job *and* the call's semantic key. Every
            # caller that makes one call per Job derives a constant key per
            # Job, for which this is the same lookup as before.
            existing = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(
                    workspace_id=ctx.workspace_id,
                    job_id=job_id,
                    call_semantic_key_hash=call_semantic_key_hash,
                )
                .order_by(ProviderCallAttemptRecord.attempt_no.desc())
                .first()
            )

            if existing is not None and existing.dispatch_state in ("succeeded", "failed"):
                # Window 4: completion already committed. Replay, never
                # re-dispatch and never re-materialize/re-fold.
                return {
                    "replay": True,
                    "attempt_id": existing.id,
                    "dispatch_certainty": (
                        "dispatched" if existing.dispatch_state == "succeeded" else "not_dispatched"
                    ),
                    "success": existing.dispatch_state == "succeeded",
                    "provider_request_id": existing.provider_request_id,
                    "evidence_reference": existing.provider_evidence_ref,
                    "error_code": existing.error_code,
                    "error_detail": existing.error_detail,
                }

            if existing is not None and existing.dispatch_state == "uncertain":
                # Window 5 recovery re-entering execute(): never blind
                # retry from here -- the caller must call reconcile().
                return {
                    "replay": True,
                    "attempt_id": existing.id,
                    "dispatch_certainty": "uncertain",
                    "success": False,
                    "provider_request_id": existing.provider_request_id,
                    "evidence_reference": existing.provider_evidence_ref,
                    "error_code": existing.error_code,
                    "error_detail": existing.error_detail or "call left uncertain; call reconcile()",
                }

            if existing is not None and existing.dispatch_state == "dispatching":
                # Window 3: the last committed evidence is "dispatch
                # started", with no result evidence -- we do not know
                # whether the network call was ever sent or answered.
                # Never redispatch blindly: classify uncertain now and
                # require explicit reconciliation.
                now = _utcnow()
                append_event(
                    session,
                    workspace_id=ctx.workspace_id,
                    provider_call_attempt_id=existing.id,
                    event_type="failure_classified",
                    payload={"classification": "uncertain", "reason": "recovered_mid_dispatch"},
                    executor=ctx.executor,
                )
                existing.dispatch_state = "uncertain"
                existing.updated_at = now
                session.flush()
                return {
                    "replay": True,
                    "attempt_id": existing.id,
                    "dispatch_certainty": "uncertain",
                    "success": False,
                    "provider_request_id": existing.provider_request_id,
                    "evidence_reference": existing.provider_evidence_ref,
                    "error_code": "dispatch_interrupted",
                    "error_detail": "recovered mid-dispatch with no result evidence; call reconcile()",
                }

            if existing is not None and existing.dispatch_state == "prepared":
                # Windows 1/2: no network attempt is confirmed to have
                # started. Safe to (re)dispatch the same authorized call.
                attempt = existing
                idempotency_key = attempt.provider_idempotency_key or (
                    f"{call_semantic_key_hash}:{attempt.attempt_no}"
                )
                if attempt.provider_idempotency_key is None:
                    attempt.provider_idempotency_key = idempotency_key
                    session.flush()
                return {
                    "replay": False,
                    "attempt_id": attempt.id,
                    "idempotency_key": idempotency_key,
                }

            # Fresh call: no attempt row exists yet for this exact job_id.
            # Only a genuinely fresh prepare needs the reservation to still
            # be 'reserved' -- validate that here, not above, so replaying
            # an already-settled/released terminal attempt never trips it.
            self._validate_chargeability(
                chargeability=chargeability,
                execution_mode=execution_mode,
                spend_reservation_id=spend_reservation_id,
            )
            if spend_reservation_id is not None:
                reservation = (
                    session.query(SpendReservationRecord)
                    .filter_by(id=spend_reservation_id, workspace_id=ctx.workspace_id)
                    .one_or_none()
                )
                if reservation is None:
                    raise ReservationInvalidError(
                        f"no SpendReservation {spend_reservation_id!r} in "
                        f"workspace {ctx.workspace_id!r}"
                    )
                if reservation.state != "reserved":
                    raise ReservationInvalidError(
                        f"reservation {spend_reservation_id!r} is in state "
                        f"{reservation.state!r}, expected 'reserved'"
                    )

            attempt_no = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(workspace_id=ctx.workspace_id, call_semantic_key_hash=call_semantic_key_hash)
                .count()
            )
            idempotency_key = f"{call_semantic_key_hash}:{attempt_no}"
            attempt = prepare_attempt(
                session,
                workspace_id=ctx.workspace_id,
                operation_id=operation_id,
                job_id=job_id,
                step_kind=step_kind,
                call_semantic_key_hash=call_semantic_key_hash,
                attempt_no=attempt_no,
                execution_mode=execution_mode,
                provider=provider,
                chargeability=chargeability,
                spend_reservation_id=spend_reservation_id,
                run_item_id=run_item_id,
                truth_package_id=truth_package_id,
                model=model,
                provider_idempotency_key=idempotency_key,
                credential_version_id=credential_version_id,
            )
            evidence_recorder.link_prepared_attempt(session, ctx, attempt=attempt)
            session.flush()
            return {
                "replay": False,
                "attempt_id": attempt.id,
                "idempotency_key": idempotency_key,
            }

    @staticmethod
    def _validate_chargeability(
        *, chargeability: str, execution_mode: str, spend_reservation_id: str | None,
    ) -> None:
        """Fail fast, before touching the DB, on the same relationship
        ``ProviderCallAttemptRecord``'s CHECK enforces for new (non-legacy)
        step kinds."""
        valid = (
            (chargeability == "charged" and execution_mode == "byok" and spend_reservation_id is not None)
            or (chargeability == "free_mock" and execution_mode == "mock" and spend_reservation_id is None)
            or (chargeability == "free_sandbox" and execution_mode in ("sandbox", "controlled_mock") and spend_reservation_id is None)
        )
        if not valid:
            raise ChargeabilityMismatchError(
                f"chargeability={chargeability!r} execution_mode={execution_mode!r} "
                f"spend_reservation_id={spend_reservation_id!r} is not a valid combination"
            )

    # -- Phase 2 + 2.5: dispatch and materialize -----------------------------

    async def _dispatch_and_complete(
        self,
        ctx: TenantContext,
        *,
        attempt_id: str,
        idempotency_key: str,
        request: RequestT | None,
        adapter: ExternalCallAdapter[RequestT, ResultT],
        evidence_recorder: ExternalCallEvidenceRecorder[ResultT],
        credential: memoryview | None,
        reconciled: bool = False,
    ) -> CoordinatedCallResult[ResultT]:
        self._mark_dispatching(ctx, attempt_id=attempt_id)

        try:
            provider_result = await adapter.execute(
                request, credential=credential, idempotency_key=idempotency_key,
            )
            dispatch_result: dict[str, Any] = {
                "success": True,
                "provider_request_id": provider_result.provider_request_id,
                "dispatch_certainty": "dispatched",
                "error_code": None,
                "error_detail": None,
                "output": provider_result.output,
                "reported_cost": getattr(provider_result, "reported_cost", None),
            }
        except ProviderDispatchError as exc:
            dispatch_result = {
                "success": False,
                "provider_request_id": None,
                "dispatch_certainty": exc.dispatch_certainty,
                "error_code": exc.error_code or "provider_error",
                "error_detail": str(exc),
                "output": None,
                "retryable": getattr(exc, "retryable", None),
                "reported_cost": getattr(exc, "reported_cost", None),
            }
        except Exception as exc:  # noqa: BLE001 -- unexpected failures are uncertain, never silently dropped
            dispatch_result = {
                "success": False,
                "provider_request_id": None,
                "dispatch_certainty": "uncertain",
                "error_code": "unexpected_error",
                "error_detail": str(exc),
                "output": None,
            }

        evidence_reference: str | None = None
        if dispatch_result["success"]:
            try:
                evidence_reference = await self._materialize(
                    evidence_recorder, ctx, attempt_id=attempt_id, output=dispatch_result["output"],
                )
            except Exception as exc:  # noqa: BLE001 -- the provider answered; only our storage failed
                # The call happened and may well have been billed, but its
                # output never reached durable storage: that is an uncertain
                # outcome to reconcile (re-materialize or write off), never a
                # silent success without evidence and never a blind retry.
                dispatch_result = {
                    "success": False,
                    "provider_request_id": dispatch_result["provider_request_id"],
                    "dispatch_certainty": "uncertain",
                    "error_code": "materialize_failed",
                    "error_detail": f"{type(exc).__name__}: {exc}"[:1000],
                    "output": None,
                    "reported_cost": dispatch_result.get("reported_cost"),
                }

        return self._complete_in_transaction(
            ctx,
            attempt_id=attempt_id,
            evidence_recorder=evidence_recorder,
            dispatch_result=dispatch_result,
            evidence_reference=evidence_reference,
            reconciled=reconciled,
        )

    @staticmethod
    async def _materialize(
        evidence_recorder: ExternalCallEvidenceRecorder[ResultT],
        ctx: TenantContext,
        *,
        attempt_id: str,
        output: ResultT,
    ) -> str:
        """Run the recorder's materialize step, sync or async.

        Placing output into durable storage is I/O: a recorder that uploads
        to an object store returns an awaitable; a recorder that only hashes
        (the controlled mock, Localization) returns the reference directly.
        """
        reference = evidence_recorder.materialize(ctx, attempt_id=attempt_id, output=output)
        if inspect.isawaitable(reference):
            reference = await reference
        return reference

    def _mark_dispatching(self, ctx: TenantContext, *, attempt_id: str) -> None:
        """A short, separate commit recording that dispatch is about to
        begin -- distinct from the (possibly long-running) network call
        itself, which never runs inside a transaction. This is what makes
        crash window 3 observable as ``dispatching`` rather than silently
        looking identical to ``prepared``."""
        with self._session_factory(ctx) as session:
            attempt = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(id=attempt_id, workspace_id=ctx.workspace_id)
                .one()
            )
            append_event(
                session,
                workspace_id=ctx.workspace_id,
                provider_call_attempt_id=attempt_id,
                event_type="dispatch_started",
                payload={},
                executor=ctx.executor,
            )
            attempt.dispatch_state = "dispatching"
            attempt.dispatching_at = _utcnow()
            attempt.updated_at = _utcnow()
            session.flush()

    # -- Phase 3: complete ----------------------------------------------------

    def _complete_in_transaction(
        self,
        ctx: TenantContext,
        *,
        attempt_id: str,
        evidence_recorder: ExternalCallEvidenceRecorder[ResultT],
        dispatch_result: dict[str, Any],
        evidence_reference: str | None,
        reconciled: bool = False,
    ) -> CoordinatedCallResult[ResultT]:
        with self._session_factory(ctx) as session:
            attempt = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(id=attempt_id, workspace_id=ctx.workspace_id)
                .one()
            )
            now = _utcnow()
            reported_cost: ReportedCost | None = dispatch_result.get("reported_cost")
            if reported_cost is not None:
                self._append_usage_event(
                    session, ctx, attempt=attempt, reported_cost=reported_cost,
                )

            if dispatch_result["success"]:
                attempt.dispatch_state = "succeeded"
                attempt.provider_request_id = dispatch_result["provider_request_id"]
                attempt.provider_evidence_ref = evidence_reference
                append_event(
                    session,
                    workspace_id=ctx.workspace_id,
                    provider_call_attempt_id=attempt_id,
                    event_type="response_received",
                    payload={"provider_request_id": dispatch_result["provider_request_id"]},
                    executor=ctx.executor,
                    evidence_refs={"reference": evidence_reference} if evidence_reference else None,
                )
                if reconciled:
                    append_event(
                        session,
                        workspace_id=ctx.workspace_id,
                        provider_call_attempt_id=attempt_id,
                        event_type="reconciled",
                        payload={"outcome": "succeeded"},
                        executor=ctx.executor,
                    )
                evidence_recorder.persist_result(
                    session, ctx,
                    attempt=attempt,
                    reference=evidence_reference,
                    output=dispatch_result["output"],
                )
                self._settle_reservation(
                    session, ctx, attempt=attempt, now=now, reported_cost=reported_cost,
                )
            elif dispatch_result["dispatch_certainty"] in ("not_dispatched", "dispatched"):
                # ``not_dispatched``: nothing was billed -- release.
                # ``dispatched``: the provider answered and billed, but the
                # answer is unusable -- the attempt still fails terminally,
                # and what it cost is settled, never released.
                billed = dispatch_result["dispatch_certainty"] == "dispatched"
                attempt.dispatch_state = "failed"
                attempt.error_code = dispatch_result["error_code"]
                attempt.error_detail = dispatch_result["error_detail"]
                payload: dict[str, Any] = {
                    "classification": "terminal",
                    "error_code": dispatch_result["error_code"],
                }
                if dispatch_result.get("retryable") is not None:
                    payload["retryable"] = bool(dispatch_result["retryable"])
                if billed:
                    payload["billed"] = True
                append_event(
                    session,
                    workspace_id=ctx.workspace_id,
                    provider_call_attempt_id=attempt_id,
                    event_type="failure_classified",
                    payload=payload,
                    executor=ctx.executor,
                )
                if billed:
                    self._settle_reservation(
                        session, ctx, attempt=attempt, now=now, reported_cost=reported_cost,
                    )
                else:
                    self._release_reservation(session, ctx, attempt=attempt, now=now)
            else:
                attempt.dispatch_state = "uncertain"
                attempt.error_code = dispatch_result["error_code"]
                attempt.error_detail = dispatch_result["error_detail"]
                append_event(
                    session,
                    workspace_id=ctx.workspace_id,
                    provider_call_attempt_id=attempt_id,
                    event_type="failure_classified",
                    payload={
                        "classification": "uncertain",
                        "error_code": dispatch_result["error_code"],
                        **(
                            {"retryable": bool(dispatch_result["retryable"])}
                            if dispatch_result.get("retryable") is not None
                            else {}
                        ),
                    },
                    executor=ctx.executor,
                )
                # Leave the reservation exactly as prepare-time left it
                # ('reserved'): we do not yet know whether the provider
                # billed this call. reconcile() is what eventually settles
                # or releases it once the outcome is known -- never guess
                # here.

            attempt.completed_at = now
            attempt.updated_at = now
            session.flush()

            return CoordinatedCallResult(
                attempt_id=attempt_id,
                dispatch_certainty=(
                    "dispatched" if dispatch_result["success"]
                    else dispatch_result["dispatch_certainty"]
                ),
                success=dispatch_result["success"],
                replayed=False,
                provider_request_id=dispatch_result["provider_request_id"],
                evidence_reference=evidence_reference,
                error_code=dispatch_result["error_code"],
                error_detail=dispatch_result["error_detail"],
                result=dispatch_result["output"] if dispatch_result["success"] else None,
            )

    @staticmethod
    def _append_usage_event(
        session: Session,
        ctx: TenantContext,
        *,
        attempt: ProviderCallAttemptRecord,
        reported_cost: ReportedCost,
    ) -> None:
        """Record the provider's usage report on the attempt.

        ``usage_reported`` feeds the projection's ``usage_summary_json``.
        It is written whether or not the call is chargeable: a mock call
        reports its simulated usage the same way.
        """
        cost: dict[str, Any] = {
            "provider_amount": format(Decimal(reported_cost.provider_amount), "f"),
            "provider_currency": reported_cost.provider_currency,
            "price_rule_version": reported_cost.price_rule_version,
        }
        if reported_cost.simulated_amount is not None:
            cost["simulated_amount"] = format(Decimal(reported_cost.simulated_amount), "f")
        append_event(
            session,
            workspace_id=ctx.workspace_id,
            provider_call_attempt_id=attempt.id,
            event_type="usage_reported",
            payload={
                "usage": dict(reported_cost.usage),
                "cost": cost,
                "evidence": dict(reported_cost.evidence),
            },
            executor=ctx.executor,
        )

    @staticmethod
    def _settle_reservation(
        session: Session,
        ctx: TenantContext,
        *,
        attempt: ProviderCallAttemptRecord,
        now: datetime,
        reported_cost: ReportedCost | None = None,
    ) -> None:
        """Settle the chargeable reservation (if any) once the provider has
        billed, mirroring ``app.entitlements.coordinator``'s completion phase.

        With a ``reported_cost`` (usage-priced providers) the ledger gets a
        ``reported_cost`` entry for what the call actually cost and a
        ``release`` entry (direction ``decrease``) for the unused part of the
        reservation. Without one, the reservation's own maximum is recorded
        as an ``estimated_settlement`` (spec §2D: "write reported or
        estimated settlement evidence, with later Adjustment when
        required")."""
        if attempt.spend_reservation_id is None:
            return
        reservation = (
            session.query(SpendReservationRecord)
            .filter_by(id=attempt.spend_reservation_id, workspace_id=ctx.workspace_id)
            .one()
        )
        reservation.state = "settled"
        reservation.outstanding_budget_amount = 0
        reservation.settled_at = now
        reservation.updated_at = now

        if reported_cost is not None:
            ExternalCallCoordinator._settle_reported_cost(
                session, ctx, attempt=attempt, reservation=reservation,
                reported_cost=reported_cost, now=now,
            )
            return

        session.add(ProviderCostLedgerEntryRecord(
            id=new_id("pcl"),
            workspace_id=ctx.workspace_id,
            operation_id=attempt.operation_id,
            job_id=attempt.job_id,
            spend_reservation_id=attempt.spend_reservation_id,
            credential_version_id=attempt.credential_version_id,
            entry_type="estimated_settlement",
            direction="increase",
            execution_mode=attempt.execution_mode,
            semantic_key_hash=attempt.call_semantic_key_hash,
            provider=attempt.provider,
            model=attempt.model,
            provider_amount=reservation.maximum_provider_amount,
            provider_currency=reservation.provider_currency,
            budget_amount=reservation.maximum_budget_amount,
            budget_currency=reservation.budget_currency,
            exchange_rate=(
                reservation.maximum_budget_amount / reservation.maximum_provider_amount
                if reservation.maximum_provider_amount
                else 1
            ),
            created_at=now,
        ))

    @staticmethod
    def _settle_reported_cost(
        session: Session,
        ctx: TenantContext,
        *,
        attempt: ProviderCallAttemptRecord,
        reservation: SpendReservationRecord,
        reported_cost: ReportedCost,
        now: datetime,
    ) -> None:
        """``reported_cost`` + ``release`` ledger rows for a usage-priced call.

        Ledger keys are namespaced hashes of the call key
        (``ledger_entry_key``), so they fit ``varchar(64)`` and never collide
        with the quote/reservation rows of the same call (PR #150). The
        reported amount is recorded as reported even if it exceeds the
        reservation -- the ledger is the truth; the note says so.
        """
        maximum_provider = Decimal(str(reservation.maximum_provider_amount))
        maximum_budget = Decimal(str(reservation.maximum_budget_amount))
        rate = (maximum_budget / maximum_provider) if maximum_provider else Decimal("1")
        if rate <= 0:
            rate = Decimal("1")
        rate = rate.quantize(_LEDGER_QUANTUM)
        actual_provider = Decimal(reported_cost.provider_amount).quantize(_LEDGER_QUANTUM)
        actual_budget = (actual_provider * rate).quantize(_LEDGER_QUANTUM)
        note = f"price_rule_version={reported_cost.price_rule_version}"
        if actual_budget > maximum_budget:
            note += "; exceeded_reservation"
        call_key = attempt.call_semantic_key_hash
        session.add(ProviderCostLedgerEntryRecord(
            id=new_id("pcl"),
            workspace_id=ctx.workspace_id,
            operation_id=attempt.operation_id,
            job_id=attempt.job_id,
            spend_reservation_id=reservation.id,
            credential_version_id=attempt.credential_version_id,
            entry_type="reported_cost",
            direction="increase",
            execution_mode=attempt.execution_mode,
            semantic_key_hash=ledger_entry_key("reported_cost", call_key),
            provider=attempt.provider,
            model=attempt.model,
            provider_amount=actual_provider,
            provider_currency=reported_cost.provider_currency,
            budget_amount=actual_budget,
            budget_currency=reservation.budget_currency,
            exchange_rate=rate,
            provider_call_attempt_id=attempt.id,
            provider_evidence_ref=attempt.provider_evidence_ref,
            note=note,
            created_at=now,
        ))
        remainder_budget = maximum_budget - actual_budget
        if remainder_budget > 0:
            session.add(ProviderCostLedgerEntryRecord(
                id=new_id("pcl"),
                workspace_id=ctx.workspace_id,
                operation_id=attempt.operation_id,
                job_id=attempt.job_id,
                spend_reservation_id=reservation.id,
                credential_version_id=attempt.credential_version_id,
                entry_type="release",
                direction="decrease",
                execution_mode=attempt.execution_mode,
                semantic_key_hash=ledger_entry_key("release", call_key),
                provider=attempt.provider,
                model=attempt.model,
                provider_amount=max(maximum_provider - actual_provider, Decimal("0")),
                provider_currency=reservation.provider_currency,
                budget_amount=remainder_budget,
                budget_currency=reservation.budget_currency,
                exchange_rate=rate,
                provider_call_attempt_id=attempt.id,
                note="unused reservation released after reported cost",
                created_at=now,
            ))

    @staticmethod
    def _release_reservation(
        session: Session, ctx: TenantContext, *, attempt: ProviderCallAttemptRecord, now: datetime,
    ) -> None:
        """On a terminal (``not_dispatched``) failure, release the
        reservation -- ``failed_no_charge``, no cost-ledger entry."""
        if attempt.spend_reservation_id is None:
            return
        reservation = (
            session.query(SpendReservationRecord)
            .filter_by(id=attempt.spend_reservation_id, workspace_id=ctx.workspace_id)
            .one()
        )
        reservation.state = "released"
        reservation.outstanding_budget_amount = 0
        reservation.released_at = now
        reservation.updated_at = now
