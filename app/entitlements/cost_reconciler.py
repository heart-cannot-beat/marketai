"""CostReconciler: provider cost reconciliation and adjustment.

Records reported provider costs against call attempts and creates
adjustment entries when costs need correction.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
)
from app.platform.tenancy import TenantContext


class CostReconciler:
    """Reconcile provider costs and record adjustments."""

    def reconcile_attempt(
        self,
        session: Session,
        context: TenantContext,
        *,
        attempt_id: str,
        reported_provider_amount: Decimal,
        reported_provider_currency: str,
        evidence_ref: str | None = None,
    ) -> str:
        """Create a reported_cost ledger entry for a provider call attempt.

        Looks up the ProviderCallAttemptRecord, verifies workspace
        ownership, and creates an increase ledger entry. Returns the
        ledger entry id.
        """
        attempt = session.get(ProviderCallAttemptRecord, attempt_id)
        if attempt is None:
            raise ValueError(f"provider call attempt {attempt_id!r} not found")
        if attempt.workspace_id != context.workspace_id:
            raise PermissionError(
                f"attempt {attempt_id!r} does not belong to workspace "
                f"{context.workspace_id!r}"
            )

        now = datetime.now(timezone.utc)
        entry_id = f"pcl_{uuid.uuid4().hex}"

        entry = ProviderCostLedgerEntryRecord(
            id=entry_id,
            workspace_id=context.workspace_id,
            operation_id=attempt.operation_id,
            job_id=attempt.job_id,
            spend_reservation_id=attempt.spend_reservation_id,
            credential_version_id=attempt.credential_version_id,
            entry_type="reported_cost",
            direction="increase",
            execution_mode=attempt.execution_mode,
            semantic_key_hash=f"rcn_{uuid.uuid4().hex[:16]}",
            provider=attempt.provider,
            model=attempt.model,
            provider_amount=reported_provider_amount,
            provider_currency=reported_provider_currency,
            budget_amount=reported_provider_amount,
            budget_currency=reported_provider_currency,
            exchange_rate=Decimal("1"),
            provider_evidence_ref=attempt.provider_evidence_ref,
            reconciliation_evidence_ref=evidence_ref,
            created_at=now,
        )
        session.add(entry)
        session.flush()
        return entry_id

    def record_adjustment(
        self,
        session: Session,
        context: TenantContext,
        *,
        original_entry_id: str,
        adjustment_amount: Decimal,
        adjustment_currency: str,
        reason: str,
        actor_id: str,
        audit_event_id: str | None = None,
    ) -> str:
        """Create an adjustment ledger entry linked to an original entry.

        Looks up the original ProviderCostLedgerEntryRecord, verifies
        workspace ownership, and creates an adjustment entry. The
        direction is 'decrease' if the adjustment reduces cost (negative
        or positive amount reducing total), 'increase' otherwise.
        Returns the new entry id.
        """
        original = session.get(ProviderCostLedgerEntryRecord, original_entry_id)
        if original is None:
            raise ValueError(
                f"original ledger entry {original_entry_id!r} not found"
            )
        if original.workspace_id != context.workspace_id:
            raise PermissionError(
                f"ledger entry {original_entry_id!r} does not belong to "
                f"workspace {context.workspace_id!r}"
            )

        now = datetime.now(timezone.utc)
        new_entry_id = f"pcl_{uuid.uuid4().hex}"

        # Determine direction: negative adjustment_amount means a cost
        # reduction (decrease), positive means adding cost (increase).
        direction = "decrease" if adjustment_amount < 0 else "increase"
        abs_amount = abs(adjustment_amount)

        entry = ProviderCostLedgerEntryRecord(
            id=new_entry_id,
            workspace_id=context.workspace_id,
            operation_id=original.operation_id,
            job_id=original.job_id,
            spend_reservation_id=original.spend_reservation_id,
            credential_version_id=original.credential_version_id,
            entry_type="adjustment",
            direction=direction,
            execution_mode=original.execution_mode,
            semantic_key_hash=f"adj_{uuid.uuid4().hex[:16]}",
            provider=original.provider,
            model=original.model,
            provider_amount=abs_amount,
            provider_currency=adjustment_currency,
            budget_amount=abs_amount,
            budget_currency=adjustment_currency,
            exchange_rate=Decimal("1"),
            adjusts_entry_id=original_entry_id,
            audit_event_id=audit_event_id,
            audit_actor_id=actor_id,
            note=reason,
            created_at=now,
        )
        session.add(entry)
        session.flush()
        return new_entry_id
