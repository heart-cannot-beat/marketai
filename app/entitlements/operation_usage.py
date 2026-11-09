"""Usage of Operations: how many settled and how long they took (Issue #8, slice S2).

``transition_operation`` calls :func:`record_operation_settled` in the
transaction that moves an Operation to a terminal state (``succeeded``,
``partially_succeeded``, ``failed`` or ``cancelled``). That writes two facts,
both keyed on the Operation id:

* ``operations``: quantity 1;
* ``processing_seconds``: the seconds from the Operation's ``created_at`` to
  the terminal transition, to the millisecond.

Terminal states have no outgoing transition, so an Operation reaches one
exactly once and is counted exactly once. ``uncertain`` is not terminal (a
reconciler can still move it back to ``running`` or on to any terminal
state), so nothing is written there; an Operation that goes ``uncertain`` and
is later settled is counted when it is settled, its seconds including the
time it spent uncertain. An Operation that never leaves ``uncertain`` is not
counted.

``operation_records`` has no execution-mode column. The mode of an
Operation is read, in the same transaction, from the rows that record the
mode of what it ran or acted on (:data:`MODE_EVIDENCE`): its provider call
attempts and spend reservations, the domain call rows of the flows that call
no provider through an attempt (Mock planning and AI QC), and the Generation
Run, Localization Batch or Marketing round its command acted on. The
strongest mode found wins: ``byok``, then ``sandbox``, then ``mock``
(``controlled_mock`` counts as ``mock``). An Operation with none of these
rows ran no provider work in any mode and is ``not_applicable``. A Mock
Operation is therefore the ``mock`` bucket of ``operations``, kept out of
``total`` like every Mock fact.

Every table read here is readable by both the API and the Worker role; the
Meta and Scale tables are not read at all, because their rows are scoped by
a Meta-mode context the Operation's session does not bind.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable

from sqlalchemy import Select, select, union
from sqlalchemy.orm import Session

from app.entitlements.usage import (
    OPERATION,
    OPERATIONS,
    PROCESSING_SECONDS,
    UsageFact,
    UsageFactError,
    UsageRecorder,
    UsageWriteResult,
    as_utc,
    usage_semantic_key,
)
from app.platform.tenancy import TenantContext

if TYPE_CHECKING:
    from app.adapters.persistence.models import OperationRecord

logger = logging.getLogger(__name__)

#: A recorded execution mode -> its Usage bucket. Values outside this map are
#: not a provider execution mode and are ignored (and logged).
_EVIDENCE_MODES = {
    "byok": "byok",
    "sandbox": "sandbox",
    "mock": "mock",
    "controlled_mock": "mock",
}

#: The order in which a mode found on an Operation's rows wins.
_PRECEDENCE = ("byok", "sandbox", "mock")

_MILLISECOND = Decimal("0.001")


@dataclass(frozen=True)
class ModeEvidence:
    """Rows of ``table`` whose ``operation_column`` names an Operation carry
    that Operation's mode in ``mode_column`` -- of ``table`` itself, or of the
    ``via_table`` row that ``via_column`` points at."""

    table: str
    operation_column: str
    mode_column: str
    via_table: str | None = None
    via_column: str | None = None


#: Every place an Operation's execution mode is recorded. ``tests/
#: test_usage_assets_operations.py`` fails when a mapped table carries an
#: Operation id and an execution mode but is neither listed here nor in
#: :data:`NOT_MODE_EVIDENCE`.
MODE_EVIDENCE: tuple[ModeEvidence, ...] = (
    ModeEvidence("provider_call_attempts", "operation_id", "execution_mode"),
    ModeEvidence("spend_reservations", "operation_id", "execution_mode"),
    ModeEvidence("marketing_planning_calls", "operation_id", "execution_mode"),
    ModeEvidence("marketing_ai_qc_calls", "operation_id", "execution_mode"),
    ModeEvidence("generation_runs", "creation_operation_id", "execution_mode"),
    ModeEvidence("competitor_scans", "operation_id", "execution_mode"),
    ModeEvidence("competitor_resolutions", "operation_id", "execution_mode"),
    ModeEvidence("product_intakes", "operation_id", "execution_mode"),
    ModeEvidence("external_creatives", "operation_id", "execution_mode"),
    ModeEvidence("export_packages", "operation_id", "execution_mode"),
    ModeEvidence("localization_item_provider_calls", "operation_id", "provider_execution_mode"),
    ModeEvidence("localization_preparation_provider_calls", "operation_id", "provider_execution_mode"),
    # Commands that act on a resource whose mode is recorded on the resource.
    ModeEvidence("generation_run_commands", "operation_id", "execution_mode",
                 via_table="generation_runs", via_column="generation_run_id"),
    ModeEvidence("localization_batch_commands", "operation_id", "provider_execution_mode",
                 via_table="localization_batches", via_column="batch_id"),
    ModeEvidence("marketing_image_edits", "operation_id", "execution_mode",
                 via_table="marketing_rounds", via_column="round_id"),
    ModeEvidence("marketing_handoffs", "operation_id", "execution_mode",
                 via_table="marketing_rounds", via_column="round_id"),
)

#: Tables that carry an Operation id and a mode column that is deliberately
#: not read, and why.
NOT_MODE_EVIDENCE: dict[str, str] = {
    "provider_cost_ledger": "BYOK-only and the Worker cannot read it; the attempt it settles is read instead",
    "scale_exports": "meta_mode is a Meta environment, not a provider execution mode; Meta-mode RLS",
    "launch_provisioning_records": "meta_mode is a Meta environment, not a provider execution mode; Meta-mode RLS",
    "launch_reconciliation_records": "meta_mode is a Meta environment, not a provider execution mode; Meta-mode RLS",
    "meta_call_intent_records": "meta_mode is a Meta environment, not a provider execution mode; Meta-mode RLS",
    "meta_call_outcome_records": "meta_mode is a Meta environment, not a provider execution mode; Meta-mode RLS",
    "meta_sync_run_records": "meta_mode is a Meta environment, not a provider execution mode; Meta-mode RLS",
    "evidence_publication_records": "meta_mode is a Meta environment, not a provider execution mode; Meta-mode RLS",
}


def mapped_tables():
    """The mapped tables, with every module that declares an evidence table loaded."""
    import app.adapters.persistence.models  # noqa: F401
    import app.competitors.models  # noqa: F401
    import app.entitlements.cost_models  # noqa: F401
    import app.generation.models  # noqa: F401
    import app.marketing.ai_qc_models  # noqa: F401
    import app.marketing.models  # noqa: F401
    import app.marketing.workflow_models  # noqa: F401
    from app.adapters.persistence.database import Base

    return Base.metadata.tables


def _evidence_statement(evidence: ModeEvidence, workspace_id: str, operation_id: str) -> Select:
    tables = mapped_tables()
    rows = tables[evidence.table]
    if evidence.via_table is None:
        return select(rows.c[evidence.mode_column].label("mode")).where(
            rows.c.workspace_id == workspace_id,
            rows.c[evidence.operation_column] == operation_id,
        )
    target = tables[evidence.via_table]
    return (
        select(target.c[evidence.mode_column].label("mode"))
        .select_from(rows)
        .join(
            target,
            (target.c.workspace_id == rows.c.workspace_id)
            & (target.c.id == rows.c[evidence.via_column]),
        )
        .where(
            rows.c.workspace_id == workspace_id,
            rows.c[evidence.operation_column] == operation_id,
        )
    )


def operation_usage_mode(session: Session, workspace_id: str, operation_id: str) -> str:
    """The Usage bucket of one Operation, from the rows that record its mode.

    One ``UNION`` statement over :data:`MODE_EVIDENCE`, in the caller's
    transaction and under its RLS context.
    """
    statement = union(*(_evidence_statement(e, workspace_id, operation_id) for e in MODE_EVIDENCE))
    modes = session.execute(statement).scalars().all()
    return strongest_mode(modes, workspace_id=workspace_id, operation_id=operation_id)


def strongest_mode(modes: Iterable[str], *, workspace_id: str, operation_id: str) -> str:
    """Fold the modes recorded for one Operation into its Usage bucket.

    ``byok`` wins over ``sandbox``, which wins over ``mock``; none is
    ``not_applicable``. A value that is not a provider execution mode is
    logged and ignored rather than failing the transition that settles the
    Operation.
    """
    found: set[str] = set()
    for mode in modes:
        bucket = _EVIDENCE_MODES.get(mode)
        if bucket is None:
            logger.warning(
                "usage_operation_mode_unrecognized",
                extra={"workspace_id": workspace_id, "operation_id": operation_id, "mode": mode},
            )
            continue
        found.add(bucket)
    for bucket in _PRECEDENCE:
        if bucket in found:
            return bucket
    return "not_applicable"


def record_operation_settled(
    session: Session,
    ctx: TenantContext,
    operation: OperationRecord,
    *,
    settled_at: datetime,
) -> UsageWriteResult:
    """Count ``operations`` and ``processing_seconds`` for an Operation just settled.

    Called by ``transition_operation`` in the transaction of the terminal
    transition, with that transition's timestamp. Both keys are the
    Operation id, so writing them again counts nothing new.
    """
    if operation.workspace_id != ctx.workspace_id:
        raise UsageFactError(
            "usage_workspace_mismatch",
            "the Operation belongs to another Workspace than the context",
        )
    settled_at = as_utc(settled_at)
    elapsed = Decimal(str((settled_at - as_utc(operation.created_at)).total_seconds()))
    if elapsed < 0:
        # created_at and settled_at may come from two hosts' clocks (the API
        # creates, the Worker settles); a skew is not a negative duration.
        logger.warning(
            "usage_processing_seconds_clock_skew",
            extra={"workspace_id": ctx.workspace_id, "operation_id": operation.id, "seconds": str(elapsed)},
        )
        elapsed = Decimal("0")
    mode = operation_usage_mode(session, ctx.workspace_id, operation.id)
    facts = [
        UsageFact(
            metric_key=OPERATIONS.key,
            quantity=Decimal("1"),
            unit=OPERATIONS.unit,
            execution_mode=mode,
            semantic_key_hash=usage_semantic_key(OPERATIONS.key, OPERATION, operation.id),
            occurred_at=settled_at,
            source_resource_type=OPERATION,
            source_resource_id=operation.id,
        ),
        UsageFact(
            metric_key=PROCESSING_SECONDS.key,
            quantity=elapsed.quantize(_MILLISECOND),
            unit=PROCESSING_SECONDS.unit,
            execution_mode=mode,
            semantic_key_hash=usage_semantic_key(PROCESSING_SECONDS.key, OPERATION, operation.id),
            occurred_at=settled_at,
            source_resource_type=OPERATION,
            source_resource_id=operation.id,
        ),
    ]
    return UsageRecorder().record_many(session, ctx, facts)
