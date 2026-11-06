"""ExternalCallCoordinator: three-phase external provider call orchestration.

Implements the dispatch fence between reserved spend and an actual provider
API call.  The three phases ensure that:

1. **prepare_in_transaction** -- credentials are decrypted and a
   ``ProviderCallAttemptRecord`` is persisted *inside* a DB transaction.
2. **dispatch** -- the network call runs *outside* any transaction so a
   long-running HTTP request cannot hold a database lock.
3. **complete_in_transaction** -- the attempt outcome, reservation
   settlement, and cost ledger entry are persisted atomically.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager

from sqlalchemy.orm import Session

from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.entitlements.credential_protector import (
    CredentialEncryptionContext,
    CredentialProtector,
)
from app.entitlements.credentials import (
    CredentialBindingRecord,
    CredentialVersionRecord,
)
from app.entitlements.types import ExternalCallOutcome
from app.platform.tenancy import TenantContext
from app.providers.adapter import (
    ProviderCallRequest,
    ProviderDispatchError,
    UnifiedProviderAdapter,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CoordinatorError(Exception):
    """Base for coordinator-specific failures."""


class LeaseExpiredError(CoordinatorError):
    """The job lease has expired before the coordinator could act."""


class MissingCredentialBindingError(CoordinatorError):
    """No credential binding exists for the requested workspace + provider."""


class ReservationInvalidError(CoordinatorError):
    """The spend reservation is not in the expected state."""


class CredentialRevokedError(CoordinatorError):
    """The credential has been revoked and cannot be used for dispatch."""


class WorkspaceLifecycleError(CoordinatorError):
    """The workspace lifecycle prevents the requested operation."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ExternalCallCoordinator:
    """Three-phase external provider call orchestration.

    Constructor:
        session_factory -- callable returning a context-managed Session
        protector       -- CredentialProtector for envelope decryption
        adapter         -- UnifiedProviderAdapter for provider dispatch
    """

    def __init__(
        self,
        session_factory: Callable[..., ContextManager[Session]],
        protector: CredentialProtector,
        adapter: UnifiedProviderAdapter,
    ) -> None:
        self._session_factory = session_factory
        self._protector = protector
        self._adapter = adapter

    # -- public entry point --------------------------------------------------

    async def execute(
        self,
        context: TenantContext,
        *,
        job_id: str,
        call_semantic_key_hash: str,
    ) -> ExternalCallOutcome:
        """Run the full three-phase dispatch cycle."""

        # Phase 1 -- inside a transaction
        prepared = self._prepare_in_transaction(
            context,
            job_id=job_id,
            call_semantic_key_hash=call_semantic_key_hash,
        )

        # Phase 2 -- outside any transaction (network call)
        dispatch_result = await self._dispatch(prepared)

        # Phase 3 -- inside a transaction
        outcome = self._complete_in_transaction(
            context,
            prepared=prepared,
            dispatch_result=dispatch_result,
        )

        return outcome

    # -- Phase 1: prepare ----------------------------------------------------

    def _prepare_in_transaction(
        self,
        context: TenantContext,
        *,
        job_id: str,
        call_semantic_key_hash: str,
    ) -> dict[str, Any]:
        """Decrypt credential, create attempt record, validate reservation."""
        with self._session_factory(context) as session:
            # Find the spend reservation for this job.
            reservation = (
                session.query(SpendReservationRecord)
                .filter_by(
                    workspace_id=context.workspace_id,
                    job_id=job_id,
                )
                .first()
            )
            if reservation is None:
                raise ReservationInvalidError(
                    f"no spend reservation found for job {job_id}"
                )

            if reservation.state != "reserved":
                raise ReservationInvalidError(
                    f"reservation {reservation.id} is in state "
                    f"{reservation.state!r}, expected 'reserved'"
                )

            # Look up the credential binding for this workspace + provider.
            provider = reservation.provider
            binding = (
                session.query(CredentialBindingRecord)
                .filter_by(
                    workspace_id=context.workspace_id,
                    provider=provider,
                )
                .first()
            )
            if binding is None:
                raise MissingCredentialBindingError(
                    f"no credential binding for workspace "
                    f"{context.workspace_id!r}, provider {provider!r}"
                )

            # Load the credential version.
            cred_version = (
                session.query(CredentialVersionRecord)
                .filter_by(
                    workspace_id=context.workspace_id,
                    id=binding.credential_version_id,
                )
                .first()
            )
            if cred_version is None:
                raise MissingCredentialBindingError(
                    f"credential version {binding.credential_version_id!r} "
                    f"not found"
                )

            # Guard: revoked or rotated credentials must not be dispatched.
            if cred_version.state != "active":
                raise CredentialRevokedError(
                    f"credential {cred_version.id!r} is in state "
                    f"{cred_version.state!r}, expected 'active'"
                )

            # Decrypt the credential.
            from app.entitlements.credential_protector import EncryptedEnvelope

            envelope = EncryptedEnvelope(
                encrypted_value=cred_version.encrypted_secret,
                encrypted_data_key=cred_version.encrypted_data_key,
                nonce=cred_version.nonce,
                encryption_key_id=cred_version.encryption_key_id,
                encryption_algorithm=cred_version.encryption_algorithm,
                aad_schema_version=cred_version.aad_schema_version,
            )
            enc_ctx = CredentialEncryptionContext(
                workspace_id=context.workspace_id,
                credential_version_id=cred_version.id,
                credential_key=cred_version.credential_key,
                provider=cred_version.provider,
                version_no=cred_version.version_no,
            )
            with self._protector.open(envelope, enc_ctx) as credential_view:
                credential_bytes = bytes(credential_view)

            # Count existing attempts for attempt_no.
            existing_attempts = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(
                    workspace_id=context.workspace_id,
                    call_semantic_key_hash=call_semantic_key_hash,
                )
                .count()
            )

            now = _utcnow()
            attempt_id = _new_id("pca")
            idempotency_key = f"{call_semantic_key_hash}:{existing_attempts}"

            attempt = ProviderCallAttemptRecord(
                id=attempt_id,
                workspace_id=context.workspace_id,
                operation_id=reservation.operation_id,
                job_id=job_id,
                spend_reservation_id=reservation.id,
                call_semantic_key_hash=call_semantic_key_hash,
                attempt_no=existing_attempts,
                execution_mode=reservation.execution_mode,
                provider=provider,
                credential_version_id=cred_version.id,
                dispatch_state="prepared",
                prepared_at=now,
                updated_at=now,
            )
            session.add(attempt)
            session.flush()

            return {
                "attempt_id": attempt_id,
                "reservation_id": reservation.id,
                "operation_id": reservation.operation_id,
                "job_id": job_id,
                "call_semantic_key_hash": call_semantic_key_hash,
                "credential_bytes": credential_bytes,
                "credential_version_id": cred_version.id,
                "provider": provider,
                "execution_mode": reservation.execution_mode,
                "idempotency_key": idempotency_key,
                "attempt_no": existing_attempts,
            }

    # -- Phase 2: dispatch ---------------------------------------------------

    async def _dispatch(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """Call the provider adapter outside any DB transaction."""
        try:
            request = ProviderCallRequest(
                prompt="",
                reference_inputs=(),
                aspect_ratio="1:1",
                image_size="1024x1024",
                model="",
                idempotency_key=prepared["idempotency_key"],
                credential=memoryview(prepared["credential_bytes"]),
            )
            result = await self._adapter.execute(request)
            return {
                "success": True,
                "provider_request_id": result.provider_request_id,
                "dispatch_certainty": "dispatched",
                "error_code": None,
                "error_detail": None,
            }
        except ProviderDispatchError as exc:
            return {
                "success": False,
                "provider_request_id": None,
                "dispatch_certainty": exc.dispatch_certainty,
                "error_code": exc.error_code or "provider_error",
                "error_detail": str(exc),
            }
        except Exception as exc:
            return {
                "success": False,
                "provider_request_id": None,
                "dispatch_certainty": "may_have_dispatched",
                "error_code": "unexpected_error",
                "error_detail": str(exc),
            }

    # -- Phase 3: complete ---------------------------------------------------

    def _complete_in_transaction(
        self,
        context: TenantContext,
        *,
        prepared: dict[str, Any],
        dispatch_result: dict[str, Any],
    ) -> ExternalCallOutcome:
        """Settle reservation, update attempt, create ledger entry."""
        with self._session_factory(context) as session:
            now = _utcnow()

            # Update the attempt record (scoped to workspace for RLS safety).
            attempt = (
                session.query(ProviderCallAttemptRecord)
                .filter_by(
                    id=prepared["attempt_id"],
                    workspace_id=context.workspace_id,
                )
                .one()
            )

            if dispatch_result["success"]:
                attempt.dispatch_state = "succeeded"
                attempt.provider_request_id = dispatch_result["provider_request_id"]
            elif dispatch_result["dispatch_certainty"] == "not_dispatched":
                attempt.dispatch_state = "failed"
            else:
                attempt.dispatch_state = "uncertain"

            attempt.error_code = dispatch_result["error_code"]
            attempt.error_detail = dispatch_result["error_detail"]
            attempt.completed_at = now
            attempt.updated_at = now

            # Update the spend reservation (scoped to workspace for RLS safety).
            reservation = (
                session.query(SpendReservationRecord)
                .filter_by(
                    id=prepared["reservation_id"],
                    workspace_id=context.workspace_id,
                )
                .one()
            )

            if dispatch_result["success"]:
                reservation.state = "settled"
                reservation.outstanding_budget_amount = 0
                reservation.settled_at = now
                reservation.updated_at = now
            elif dispatch_result["dispatch_certainty"] == "not_dispatched":
                reservation.state = "released"
                reservation.outstanding_budget_amount = 0
                reservation.released_at = now
                reservation.updated_at = now
            else:
                reservation.state = "uncertain"
                reservation.updated_at = now

            # Create a cost ledger entry for settlements.
            if dispatch_result["success"]:
                ledger_entry = ProviderCostLedgerEntryRecord(
                    id=_new_id("pcl"),
                    workspace_id=context.workspace_id,
                    operation_id=prepared["operation_id"],
                    job_id=prepared["job_id"],
                    spend_reservation_id=prepared["reservation_id"],
                    credential_version_id=prepared["credential_version_id"],
                    entry_type="estimated_settlement",
                    direction="increase",
                    execution_mode=prepared["execution_mode"],
                    semantic_key_hash=prepared["call_semantic_key_hash"],
                    provider=prepared["provider"],
                    provider_amount=reservation.maximum_provider_amount,
                    provider_currency=reservation.provider_currency,
                    budget_amount=reservation.maximum_budget_amount,
                    budget_currency=reservation.budget_currency,
                    exchange_rate=reservation.maximum_budget_amount / reservation.maximum_provider_amount
                    if reservation.maximum_provider_amount
                    else 1,
                    created_at=now,
                )
                session.add(ledger_entry)

            session.flush()

            return ExternalCallOutcome(
                attempt_id=prepared["attempt_id"],
                call_semantic_key_hash=prepared["call_semantic_key_hash"],
                dispatch_certainty=dispatch_result["dispatch_certainty"],
                success=dispatch_result["success"],
                provider_request_id=dispatch_result["provider_request_id"],
                error_code=dispatch_result["error_code"],
                error_detail=dispatch_result["error_detail"],
            )
