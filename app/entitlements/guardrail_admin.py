"""GuardrailAdmin: spend guardrail configuration management.

Manages workspace spend guardrails with preview/confirm two-phase update
pattern, optimistic concurrency via revision checks, and platform maximum
validation.

A preview is a stored row (#386), not process state: its random id is the
``preview_token``, and it is bound to its Workspace, its creator and the
guardrail revision it was made against. Confirm consumes it once and moves
the guardrail from that revision to the next, each through a compare-and-set
``UPDATE``, so a restart, another replica or a concurrent confirm cannot
lose, reuse or double-apply a preview.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import config
from app.adapters.persistence.models import new_id
from app.entitlements.cost_models import (
    SpendGuardrailEventRecord,
    SpendGuardrailPreviewRecord,
    SpendGuardrailRecord,
)
from app.platform.tenancy import TenantContext


# Limit field names that can appear in new_limits
_LIMIT_FIELDS = (
    "per_operation_limit",
    "rolling_24h_limit",
    "period_limit",
    "outstanding_exposure_limit",
)

# Platform maximums keyed by limit field name
_PLATFORM_MAXIMUMS = {
    "per_operation_limit": lambda: config.MARKETAI_PLATFORM_MAX_PER_OPERATION,
    "rolling_24h_limit": lambda: config.MARKETAI_PLATFORM_MAX_ROLLING_24H,
    "period_limit": lambda: config.MARKETAI_PLATFORM_MAX_PERIOD,
    "outstanding_exposure_limit": lambda: config.MARKETAI_PLATFORM_MAX_OUTSTANDING,
}

# How long a preview can be confirmed, as for Scale's previews.
_PREVIEW_TTL = timedelta(minutes=15)


class GuardrailAdminError(ValueError):
    """A refused guardrail read/update; ``code``/``params`` are what the
    interface renders (#253), the message is a diagnostic."""

    def __init__(self, message: str, *, code: str, params: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.params = params or {}


def _require_owner(context: TenantContext) -> None:
    if context.role != "owner" and context.initiator_role != "owner":
        raise PermissionError("owner role required")


def _actor(context: TenantContext) -> str:
    return context.initiator_id or context.executor


def _check_platform_maximum(field: str, value: Decimal) -> None:
    platform_max = _PLATFORM_MAXIMUMS[field]()
    if value > platform_max:
        raise GuardrailAdminError(
            f"{field} ({value}) exceeds platform maximum ({platform_max})",
            code="spend_guardrail_limit_above_platform",
            params={"field": field, "limit": str(platform_max)},
        )


def _aware(value: datetime) -> datetime:
    """SQLite hands timezone-aware columns back naive; they were written UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class GuardrailAdmin:
    """Manage workspace spend guardrails."""

    def get(
        self,
        session: Session,
        context: TenantContext,
    ) -> dict:
        """Return the current guardrail configuration for the workspace."""
        record = session.get(SpendGuardrailRecord, context.workspace_id)
        if record is None:
            raise GuardrailAdminError(
                f"no guardrail found for workspace {context.workspace_id!r}",
                code="spend_guardrail_not_found",
            )
        return {
            "workspace_id": record.workspace_id,
            "revision": record.revision,
            "budget_currency": record.budget_currency,
            "per_operation_limit": record.per_operation_limit,
            "rolling_24h_limit": record.rolling_24h_limit,
            "period_limit": record.period_limit,
            "outstanding_exposure_limit": record.outstanding_exposure_limit,
            "platform_policy_version": record.platform_policy_version,
        }

    def preview_update(
        self,
        session: Session,
        context: TenantContext,
        *,
        new_limits: dict,
        expected_revision: int,
        mfa_evidence_ref: str | None,
    ) -> dict:
        """Validate and preview a guardrail update without applying it.

        Stores the proposal and returns a dict with old values, proposed
        new values, the ``preview_token`` that ``confirm_update`` must
        present to apply the change, and when it expires.
        """
        _require_owner(context)

        record = session.get(SpendGuardrailRecord, context.workspace_id)
        if record is None:
            raise GuardrailAdminError(
                f"no guardrail found for workspace {context.workspace_id!r}",
                code="spend_guardrail_not_found",
            )

        # Optimistic concurrency: revision must match
        if record.revision != expected_revision:
            raise GuardrailAdminError(
                f"revision mismatch: expected {expected_revision}, "
                f"current is {record.revision}",
                code="revision_conflict",
            )

        # Parse and validate each limit
        parsed: dict[str, Decimal] = {}
        for field in _LIMIT_FIELDS:
            raw = new_limits.get(field)
            if raw is None:
                raise GuardrailAdminError(
                    f"missing required limit field: {field}",
                    code="spend_guardrail_limit_required", params={"field": field},
                )
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError):
                raise GuardrailAdminError(
                    f"invalid decimal for {field}: {raw!r}",
                    code="spend_guardrail_limit_invalid", params={"field": field},
                )
            if value < 0:
                raise GuardrailAdminError(
                    f"{field} must be >= 0, got {value}",
                    code="spend_guardrail_limit_invalid", params={"field": field},
                )
            _check_platform_maximum(field, value)
            parsed[field] = value

        # Build old values snapshot
        old_values = {
            "per_operation_limit": str(record.per_operation_limit),
            "rolling_24h_limit": str(record.rolling_24h_limit),
            "period_limit": str(record.period_limit),
            "outstanding_exposure_limit": str(record.outstanding_exposure_limit),
        }

        now = datetime.now(timezone.utc)
        preview = SpendGuardrailPreviewRecord(
            id=new_id("sgp"),
            workspace_id=context.workspace_id,
            creator_id=_actor(context),
            base_revision=record.revision,
            created_at=now,
            expires_at=now + _PREVIEW_TTL,
            **parsed,
        )
        session.add(preview)
        session.flush()

        return {
            "workspace_id": context.workspace_id,
            "current_revision": record.revision,
            "old_limits": old_values,
            "proposed_limits": parsed,
            "preview_token": preview.id,
            "expires_at": preview.expires_at,
            "mfa_evidence_ref": mfa_evidence_ref,
        }

    def confirm_update(
        self,
        session: Session,
        context: TenantContext,
        *,
        preview_token: str,
        expected_revision: int,
    ) -> dict:
        """Apply a previously previewed guardrail update.

        The preview must be this Workspace's, made by this actor, unconsumed,
        unexpired and made against the current revision, which the client
        must name too. Consumes it, applies the changes, writes an event
        record, and returns the updated guardrail dict.
        """
        _require_owner(context)

        record = session.get(SpendGuardrailRecord, context.workspace_id)
        if record is None:
            raise GuardrailAdminError(
                f"no guardrail found for workspace {context.workspace_id!r}",
                code="spend_guardrail_not_found",
            )

        # Another Workspace's or another actor's preview is as absent as an
        # unknown token (RLS hides the former on PostgreSQL).
        preview = session.scalar(
            select(SpendGuardrailPreviewRecord).where(
                SpendGuardrailPreviewRecord.id == preview_token,
                SpendGuardrailPreviewRecord.workspace_id == context.workspace_id,
                SpendGuardrailPreviewRecord.creator_id == _actor(context),
            )
        )
        if preview is None:
            raise GuardrailAdminError(
                "preview_token does not match any pending preview; "
                "call preview_update first",
                code="spend_guardrail_preview_missing",
            )
        if preview.consumed_at is not None:
            raise GuardrailAdminError(
                "this preview has already been confirmed",
                code="preview_already_consumed",
            )

        now = datetime.now(timezone.utc)
        if _aware(preview.expires_at) <= now:
            raise GuardrailAdminError(
                "this preview has expired; preview the change again",
                code="preview_expired",
            )

        # Optimistic concurrency: the client, the preview and the guardrail
        # must all name the same revision.
        if not (expected_revision == preview.base_revision == record.revision):
            raise GuardrailAdminError(
                f"revision mismatch: expected {expected_revision}, preview was "
                f"made at {preview.base_revision}, current is {record.revision}",
                code="revision_conflict",
            )

        # The platform maximum may have been lowered since the preview.
        new_limits = {field: getattr(preview, field) for field in _LIMIT_FIELDS}
        for field, value in new_limits.items():
            _check_platform_maximum(field, value)

        old_values = {
            "per_operation_limit": str(record.per_operation_limit),
            "rolling_24h_limit": str(record.rolling_24h_limit),
            "period_limit": str(record.period_limit),
            "outstanding_exposure_limit": str(record.outstanding_exposure_limit),
        }
        old_revision = preview.base_revision
        new_revision = old_revision + 1
        actor = _actor(context)

        # Consume the preview, then move the guardrail on from the revision
        # it was made at. A concurrent confirm that got there first leaves
        # either UPDATE matching no row; raising rolls the other one back.
        consumed = session.execute(
            update(SpendGuardrailPreviewRecord)
            .where(
                SpendGuardrailPreviewRecord.id == preview.id,
                SpendGuardrailPreviewRecord.workspace_id == context.workspace_id,
                SpendGuardrailPreviewRecord.consumed_at.is_(None),
            )
            .values(consumed_at=now, result_revision=new_revision)
            .execution_options(synchronize_session=False)
        )
        if consumed.rowcount != 1:
            raise GuardrailAdminError(
                "this preview was confirmed by a concurrent request",
                code="preview_already_consumed",
            )
        moved = session.execute(
            update(SpendGuardrailRecord)
            .where(
                SpendGuardrailRecord.workspace_id == context.workspace_id,
                SpendGuardrailRecord.revision == old_revision,
            )
            .values(
                **new_limits,
                revision=new_revision,
                updated_at=now,
                updated_by=actor,
            )
            .execution_options(synchronize_session=False)
        )
        if moved.rowcount != 1:
            raise GuardrailAdminError(
                f"revision mismatch: the guardrail moved on from {old_revision}",
                code="revision_conflict",
            )
        session.refresh(record)

        new_values = {
            "per_operation_limit": str(record.per_operation_limit),
            "rolling_24h_limit": str(record.rolling_24h_limit),
            "period_limit": str(record.period_limit),
            "outstanding_exposure_limit": str(record.outstanding_exposure_limit),
        }

        # Write guardrail event
        event = SpendGuardrailEventRecord(
            id=f"sge_{uuid.uuid4().hex}",
            workspace_id=context.workspace_id,
            revision_before=old_revision,
            revision_after=record.revision,
            old_values_json=old_values,
            new_values_json=new_values,
            old_budget_currency=record.budget_currency,
            new_budget_currency=record.budget_currency,
            reason="guardrail_update",
            actor_id=actor,
            created_at=now,
        )
        session.add(event)
        session.flush()

        return self.get(session, context)
