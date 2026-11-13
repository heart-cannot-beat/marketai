"""Usage: the append-only facts of what a Workspace used (contract §9, ADR 0005).

``UsageRecorder.record_many`` appends ``UsageFact`` rows to ``usage_events``.
Every fact is checked against the metric registry (``METRICS``) before any
row is written. The registry fixes each metric's unit, measurement kind,
direction and aggregation, so a caller cannot invent a metric or a unit.

A fact is written in the transaction of the domain change it records (spec
§7.0: "Mutation fails if the fact cannot commit"). Writes are idempotent per
``(workspace_id, semantic_key_hash)``: a second write of the same key is a
no-op that is counted and logged as ``usage_event_duplicate``, never an
IntegrityError that would roll the caller's settlement back.

The Worker role holds INSERT on ``usage_events`` but no SELECT, which shapes
the statement (``tests/test_usage_pg.py`` proves both points as that role):

* ``ON CONFLICT DO NOTHING`` names no conflict target. PostgreSQL requires
  SELECT on the columns of a named target, so
  ``ON CONFLICT (workspace_id, semantic_key_hash)`` is refused for the
  Worker. Without a target any unique violation is skipped; the table's
  only unique keys are that pair and the random ``id``, and
  ``tests/test_usage_provider_calls.py`` fails if another is added.
* The outcome is read from ``rowcount`` (kept with ``preserve_rowcount``),
  never ``RETURNING``, which would need SELECT too.

Writers, each called in the transaction of the fact it records:

* ``record_provider_call`` (slice S1) counts a provider call once the
  settlement that knows the call happened commits.
* ``record_asset_version_generated`` and ``record_generation_item_failed``
  (slice S2) count a generated AssetVersion where Generation creates it, and
  a Generation RunItem that failed without one where it is marked failed.
* ``app.entitlements.operation_usage.record_operation_settled`` (slice S2)
  counts an Operation and its processing seconds when
  ``transition_operation`` moves it to a terminal state.
* ``app.entitlements.storage_usage.record_storage_bytes`` (#148) states a
  Workspace's stored bytes once per UTC day, from a Worker Job.

``UsageQuery.summarize`` folds one metric over a time window into the four
execution-mode buckets, the way the metric's ``aggregation`` says: a delta
metric's events are summed, a gauge's latest event is its level. Mock is
reported in its own bucket and never enters ``total`` (spec §5.6).
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.entitlements.usage_models import UsageEventRecord
from app.platform.tenancy import TenantContext, is_postgres

if TYPE_CHECKING:
    from app.adapters.persistence.models import AssetVersionRecord
    from app.entitlements.cost_models import ProviderCallAttemptRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------

#: The execution-mode buckets of a usage event (spec §2.12).
USAGE_EXECUTION_MODES = ("byok", "sandbox", "mock", "not_applicable")

#: The buckets that count as real use. Mock is left out (spec §5.6).
REAL_USAGE_MODES = ("byok", "sandbox", "not_applicable")


@dataclass(frozen=True)
class UsageMetric:
    """How one metric is measured and folded.

    ``measurement_kind`` is ``delta`` (each event adds to a total) or
    ``gauge`` (each event states a level). A delta's ``direction`` is
    ``increase``; a gauge's is ``set``. ``aggregation`` is how a window
    folds the events: ``sum`` or ``latest`` (``UsageQuery.summarize``).
    ``execution_modes`` are the buckets a fact of the metric may name; a
    level that no execution produced, such as stored bytes, names
    ``not_applicable`` only.
    """

    key: str
    measurement_kind: str
    direction: str
    unit: str
    aggregation: str
    execution_modes: tuple[str, ...] = USAGE_EXECUTION_MODES


#: One call to an AI or data provider that is known to have reached it.
PROVIDER_CALLS = UsageMetric(
    key="provider_calls",
    measurement_kind="delta",
    direction="increase",
    unit="call",
    aggregation="sum",
)

#: One AssetVersion that Generation produced (``origin_kind="generation"``),
#: in the transaction that creates it. A RunItem that reuses an existing
#: version produced nothing new and is not counted.
ASSET_VERSIONS_GENERATED = UsageMetric(
    key="asset_versions_generated",
    measurement_kind="delta",
    direction="increase",
    unit="asset_version",
    aggregation="sum",
)

#: One Generation RunItem that was marked ``failed`` without an AssetVersion,
#: in the transaction that marks it. A failed automatic quality check is a
#: quality outcome of a version that *was* produced, and is not counted.
GENERATION_ITEMS_FAILED = UsageMetric(
    key="generation_items_failed",
    measurement_kind="delta",
    direction="increase",
    unit="run_item",
    aggregation="sum",
)

#: One Operation that reached a terminal state. Its execution-mode bucket
#: tells Mock Operations apart; they never enter ``total``.
OPERATIONS = UsageMetric(
    key="operations",
    measurement_kind="delta",
    direction="increase",
    unit="operation",
    aggregation="sum",
)

#: Seconds from an Operation's creation to its terminal state (spec §3.11).
PROCESSING_SECONDS = UsageMetric(
    key="processing_seconds",
    measurement_kind="delta",
    direction="increase",
    unit="second",
    aggregation="sum",
)

#: A Workspace's stored bytes, stated once per UTC day (#148, contract §9
#: "私有存储字节数"). Each event is the level at its ``occurred_at``; a window
#: reports its latest event, never a sum. ``app.entitlements.storage_usage``
#: writes it.
STORAGE_BYTES = UsageMetric(
    key="storage_bytes",
    measurement_kind="gauge",
    direction="set",
    unit="byte",
    aggregation="latest",
    execution_modes=("not_applicable",),
)

#: Every metric Usage records. A metric that is not here cannot be written.
METRICS: Mapping[str, UsageMetric] = MappingProxyType({
    metric.key: metric
    for metric in (
        PROVIDER_CALLS,
        ASSET_VERSIONS_GENERATED,
        GENERATION_ITEMS_FAILED,
        OPERATIONS,
        PROCESSING_SECONDS,
        STORAGE_BYTES,
    )
})

#: A provider call attempt's execution mode -> its Usage bucket.
#:
#: ``controlled_mock`` is counted as ``mock``. ``cost_models`` puts it with
#: ``sandbox`` under the ``free_sandbox`` chargeability, but that grouping
#: answers a cost question: neither is billed. Usage answers a different one:
#: was this real use? Controlled Mock replaces the provider with a simulator
#: (CONTEXT.md, 受控 Mock), so its calls stay out of the real total like
#: every other Mock call.
_ATTEMPT_MODES: Mapping[str, str] = MappingProxyType({
    "byok": "byok",
    "sandbox": "sandbox",
    "mock": "mock",
    "controlled_mock": "mock",
})

#: ``source_resource_type`` of each kind of fact.
PROVIDER_CALL_ATTEMPT = "provider_call_attempt"
ASSET_VERSION = "asset_version"
RUN_ITEM = "run_item"
OPERATION = "operation"
WORKSPACE = "workspace"

_SEMANTIC_KEY_HASH = re.compile(r"[0-9a-f]{64}")


class UsageFactError(ValueError):
    """A fact the registry refuses. Nothing of its batch is written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class UsageFact:
    """One usage fact of the caller's Workspace, before it is written.

    The Workspace is never part of the fact: ``record_many`` takes it from
    the TenantContext. ``occurred_at`` is when the recorded thing happened,
    taken from the caller's transaction, never the time of the write.
    """

    metric_key: str
    quantity: Decimal
    unit: str
    execution_mode: str
    semantic_key_hash: str
    occurred_at: datetime
    source_resource_type: str | None = None
    source_resource_id: str | None = None


@dataclass(frozen=True)
class UsageWriteResult:
    """How many facts were new and how many were already recorded."""

    written: int
    duplicates: int


def usage_semantic_key(
    metric_key: str,
    source_resource_type: str,
    source_resource_id: str,
    discriminator: str | None = None,
) -> str:
    """``sha256("usage:v1:{metric}:{source_type}:{source_id}[:{discriminator}]")`` in hex.

    The Workspace is already in the unique constraint, so it is not part of
    the key. ``v1`` leaves room to change the rule later.
    """
    parts = ["usage", "v1", metric_key, source_resource_type, source_resource_id]
    if discriminator is not None:
        parts.append(discriminator)
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()


def usage_execution_mode(attempt_execution_mode: str) -> str:
    """The Usage bucket of a provider call attempt's execution mode."""
    try:
        return _ATTEMPT_MODES[attempt_execution_mode]
    except KeyError:
        raise UsageFactError(
            "usage_execution_mode_unknown",
            f"no usage bucket for execution mode {attempt_execution_mode!r}",
        ) from None


def validate_fact(fact: UsageFact) -> UsageMetric:
    """Check one fact against the registry; return its metric."""
    metric = METRICS.get(fact.metric_key)
    if metric is None:
        raise UsageFactError("usage_metric_unknown", f"metric {fact.metric_key!r} is not registered")
    if fact.unit != metric.unit:
        raise UsageFactError(
            "usage_unit_mismatch",
            f"metric {metric.key!r} is measured in {metric.unit!r}, not {fact.unit!r}",
        )
    quantity = fact.quantity
    if not isinstance(quantity, Decimal) or not quantity.is_finite():
        raise UsageFactError("usage_quantity_invalid", "quantity must be a finite Decimal")
    if quantity < 0:
        raise UsageFactError("usage_quantity_negative", f"quantity {quantity} is negative")
    if fact.execution_mode not in USAGE_EXECUTION_MODES:
        raise UsageFactError(
            "usage_execution_mode_unknown",
            f"execution mode {fact.execution_mode!r} is not one of {USAGE_EXECUTION_MODES}",
        )
    if fact.execution_mode not in metric.execution_modes:
        raise UsageFactError(
            "usage_execution_mode_mismatch",
            f"metric {metric.key!r} is recorded as {metric.execution_modes}, not {fact.execution_mode!r}",
        )
    if not isinstance(fact.semantic_key_hash, str) or not _SEMANTIC_KEY_HASH.fullmatch(fact.semantic_key_hash):
        raise UsageFactError("usage_semantic_key_invalid", "semantic_key_hash must be 64 lowercase hex digits")
    if fact.occurred_at.tzinfo is None:
        raise UsageFactError("usage_occurred_at_naive", "occurred_at must carry a timezone")
    return metric


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

class UsageRecorder:
    """Append usage facts to ``usage_events``."""

    def record_many(
        self,
        session: Session,
        context: TenantContext,
        facts: Iterable[UsageFact],
    ) -> UsageWriteResult:
        """Write each fact once; a fact whose key is already recorded is a duplicate.

        Every fact is validated before the first row is written, so a
        refused fact leaves the batch unwritten. Runs in the caller's
        transaction and never commits.
        """
        batch = tuple(facts)
        metrics = [validate_fact(fact) for fact in batch]
        dialect = postgresql if is_postgres(session) else sqlite
        created_at = datetime.now(timezone.utc)
        written = duplicates = 0
        for fact, metric in zip(batch, metrics):
            statement = (
                dialect.insert(UsageEventRecord.__table__)
                .values(
                    id=f"ue_{uuid.uuid4().hex}",
                    workspace_id=context.workspace_id,
                    metric_key=metric.key,
                    measurement_kind=metric.measurement_kind,
                    direction=metric.direction,
                    quantity=fact.quantity,
                    unit=metric.unit,
                    execution_mode=fact.execution_mode,
                    source_resource_type=fact.source_resource_type,
                    source_resource_id=fact.source_resource_id,
                    semantic_key_hash=fact.semantic_key_hash,
                    occurred_at=fact.occurred_at,
                    created_at=created_at,
                )
                # No conflict target: naming one needs SELECT (module docstring).
                .on_conflict_do_nothing()
            )
            # SQLAlchemy keeps an INSERT's rowcount only when asked; psycopg
            # reports -1 once the cursor is closed.
            outcome = session.execute(statement, execution_options={"preserve_rowcount": True})
            if outcome.rowcount == 1:
                written += 1
                continue
            duplicates += 1
            logger.info(
                "usage_event_duplicate",
                extra={
                    "workspace_id": context.workspace_id,
                    "metric_key": metric.key,
                    "semantic_key_hash": fact.semantic_key_hash,
                    "source_resource_type": fact.source_resource_type,
                    "source_resource_id": fact.source_resource_id,
                },
            )
        return UsageWriteResult(written=written, duplicates=duplicates)


def record_provider_call(
    session: Session,
    ctx: TenantContext,
    attempt: ProviderCallAttemptRecord,
    *,
    occurred_at: datetime,
) -> UsageWriteResult:
    """Count one ``provider_calls`` for an attempt known to have reached its provider.

    Call it in the transaction that settles the attempt, and only on a path
    that knows the provider was called: a success, or a failure the
    provider answered (and may have billed). A call that never left
    (``not_dispatched``) or whose outcome is unknown (``uncertain``) is not
    counted; reconciling it later counts it then. The key is the attempt id,
    so replaying a settlement counts nothing new.
    """
    if attempt.workspace_id != ctx.workspace_id:
        raise UsageFactError(
            "usage_workspace_mismatch",
            "the attempt belongs to another Workspace than the context",
        )
    fact = UsageFact(
        metric_key=PROVIDER_CALLS.key,
        quantity=Decimal("1"),
        unit=PROVIDER_CALLS.unit,
        execution_mode=usage_execution_mode(attempt.execution_mode),
        semantic_key_hash=usage_semantic_key(PROVIDER_CALLS.key, PROVIDER_CALL_ATTEMPT, attempt.id),
        occurred_at=occurred_at,
        source_resource_type=PROVIDER_CALL_ATTEMPT,
        source_resource_id=attempt.id,
    )
    return UsageRecorder().record_many(session, ctx, [fact])


def record_asset_version_generated(
    session: Session,
    ctx: TenantContext,
    version: AssetVersionRecord,
) -> UsageWriteResult:
    """Count one ``asset_versions_generated`` for a version Generation just created.

    Call it in the transaction that inserts the version, after the flush
    that gave it its ``created_at``. The key is the version id: a version is
    created once, so nothing can count it twice.
    """
    if version.workspace_id != ctx.workspace_id:
        raise UsageFactError(
            "usage_workspace_mismatch",
            "the AssetVersion belongs to another Workspace than the context",
        )
    if version.origin_kind != "generation" or not version.execution_mode:
        raise UsageFactError(
            "usage_source_invalid",
            "only an AssetVersion produced by Generation, with its execution mode, is counted",
        )
    fact = UsageFact(
        metric_key=ASSET_VERSIONS_GENERATED.key,
        quantity=Decimal("1"),
        unit=ASSET_VERSIONS_GENERATED.unit,
        execution_mode=usage_execution_mode(version.execution_mode),
        semantic_key_hash=usage_semantic_key(ASSET_VERSIONS_GENERATED.key, ASSET_VERSION, version.id),
        occurred_at=as_utc(version.created_at),
        source_resource_type=ASSET_VERSION,
        source_resource_id=version.id,
    )
    return UsageRecorder().record_many(session, ctx, [fact])


def record_generation_item_failed(
    session: Session,
    ctx: TenantContext,
    *,
    run_item_id: str,
    revision: int,
    execution_mode: str,
    occurred_at: datetime,
) -> UsageWriteResult:
    """Count one ``generation_items_failed`` for a RunItem just marked ``failed``.

    ``revision`` is the RunItem revision the failure wrote. A RunItem can be
    retried after it failed and fail again; each failure is its own fact,
    and the revision tells them apart. Writing the same failure again (the
    same RunItem and revision) counts nothing new.
    """
    fact = UsageFact(
        metric_key=GENERATION_ITEMS_FAILED.key,
        quantity=Decimal("1"),
        unit=GENERATION_ITEMS_FAILED.unit,
        execution_mode=usage_execution_mode(execution_mode),
        semantic_key_hash=usage_semantic_key(
            GENERATION_ITEMS_FAILED.key, RUN_ITEM, run_item_id, f"revision:{revision}",
        ),
        occurred_at=occurred_at,
        source_resource_type=RUN_ITEM,
        source_resource_id=run_item_id,
    )
    return UsageRecorder().record_many(session, ctx, [fact])


def as_utc(moment: datetime) -> datetime:
    """``moment`` with a timezone. SQLite hands back naive UTC timestamps."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageSummary:
    """One metric over one window: the real total and every bucket."""

    total: Decimal
    by_execution_mode: Mapping[str, Decimal]


class UsageQuery:
    """Fold one metric's events within a time window."""

    def summarize(
        self,
        session: Session,
        context: TenantContext,
        *,
        metric_key: str,
        window_start: datetime,
        window_end: datetime,
    ) -> UsageSummary:
        """Fold the metric over ``[window_start, window_end)`` per execution mode.

        ``by_execution_mode`` always has the four buckets; ``mock`` is never
        part of ``total``. The metric's ``aggregation`` decides the fold:

        * ``sum`` (every delta metric, and a key the registry does not
          know): each bucket sums its events, and ``total`` sums the real
          buckets ``byok``, ``sandbox`` and ``not_applicable``;
        * ``latest`` (a gauge such as ``storage_bytes``): each bucket is its
          latest event's quantity, and ``total`` is the quantity of the
          latest event in a real bucket. Events are ordered by
          ``occurred_at``, then ``created_at``, then ``id``. A window with no
          event reports ``0``.
        """
        metric = METRICS.get(metric_key)
        aggregation = metric.aggregation if metric is not None else "sum"
        in_window = (
            UsageEventRecord.workspace_id == context.workspace_id,
            UsageEventRecord.metric_key == metric_key,
            UsageEventRecord.execution_mode.in_(USAGE_EXECUTION_MODES),
            UsageEventRecord.occurred_at >= window_start,
            UsageEventRecord.occurred_at < window_end,
        )
        if aggregation == "sum":
            return self._sum(session, in_window)
        if aggregation == "latest":
            return self._latest(session, in_window)
        raise ValueError(f"metric {metric_key!r} folds by {aggregation!r}, which no query implements")

    @staticmethod
    def _sum(session: Session, in_window: tuple) -> UsageSummary:
        rows = session.execute(
            select(UsageEventRecord.execution_mode, func.sum(UsageEventRecord.quantity))
            .where(*in_window)
            .group_by(UsageEventRecord.execution_mode)
        ).all()
        buckets = {mode: Decimal("0") for mode in USAGE_EXECUTION_MODES}
        for mode, quantity in rows:
            if quantity is not None:
                buckets[mode] = Decimal(str(quantity))
        total = sum((buckets[mode] for mode in REAL_USAGE_MODES), Decimal("0"))
        return UsageSummary(total=total, by_execution_mode=MappingProxyType(buckets))

    @staticmethod
    def _latest(session: Session, in_window: tuple) -> UsageSummary:
        order = (
            UsageEventRecord.occurred_at.desc(),
            UsageEventRecord.created_at.desc(),
            UsageEventRecord.id.desc(),
        )
        ranked = (
            select(
                UsageEventRecord.execution_mode,
                UsageEventRecord.quantity,
                func.row_number()
                .over(partition_by=UsageEventRecord.execution_mode, order_by=order)
                .label("recency"),
            )
            .where(*in_window)
            .subquery()
        )
        rows = session.execute(
            select(ranked.c.execution_mode, ranked.c.quantity).where(ranked.c.recency == 1)
        ).all()
        buckets = {mode: Decimal("0") for mode in USAGE_EXECUTION_MODES}
        for mode, quantity in rows:
            buckets[mode] = Decimal(str(quantity))
        real_latest = session.scalar(
            select(UsageEventRecord.quantity)
            .where(*in_window, UsageEventRecord.execution_mode.in_(REAL_USAGE_MODES))
            .order_by(*order)
            .limit(1)
        )
        total = Decimal(str(real_latest)) if real_latest is not None else Decimal("0")
        return UsageSummary(total=total, by_execution_mode=MappingProxyType(buckets))
