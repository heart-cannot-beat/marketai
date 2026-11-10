# app/meta/observation.py
"""Observation contract generations and the 14-day coverage ledger (spec §6.1, §6.6-§6.7).

Three operations:

* :func:`create_contract_generation` -- creates an immutable
  ``ObservationContractGenerationRecord``. Per §6.7, creating the *first*
  generation for a ``(workspace_id, meta_mode, meta_connection_id,
  contract_type)`` scope cannot rely on ``SELECT ... FOR UPDATE`` (there is
  no row yet to lock), so a transaction-scoped Postgres advisory lock keyed
  on the scope is acquired first. This serializes concurrent "create the
  first generation" attempts without a row to contend over. On SQLite
  (tests, local dev) advisory locks do not exist; the partial unique index
  ``uq_ocg_active_scope`` on ``ObservationContractGenerationRecord``
  (``state = 'active'``) is the backstop that still prevents two active
  generations for the same scope there.
* :func:`record_coverage` -- appends an ``ObservationCoverageLedgerRecord``
  for one ``(contract_generation_id, coverage_date)``, and starts the
  generation's observation clock (``clock_started_at``) on the first entry
  (§6.6.2).
* :func:`calculate_tenure` -- derives tenure from the ledger as the longest
  run of consecutive ``coverage_date`` values (§6.6.3, §6.6.5) rather than a
  stored, mutable counter.

A generation's sync schedule (spec §3.14, #107) lives here too, because it is
part of the generation's lifecycle:

* :func:`ensure_observation_schedule` -- idempotently creates the one active
  ``MetaObservationSyncScheduleRecord`` of a generation, with ``next_fire_at``
  the next daily slot in the generation's frozen ``local_timezone``.
  :func:`create_contract_generation` calls it for every
  ``customer_sandbox`` generation the scheduled executor
  (``app.meta.observation_sync``) can read -- one with a
  :func:`observation_read_plan`. ``controlled_mock`` generations are never
  scheduled automatically (ADR 0009: fault injection only, no evidence for
  decisions); tests and e2e call this function for them explicitly.
  ``internal_validation`` waits for its read adapter (#101 S6). Every open
  schedule of one connection shares one cron expression and one time zone:
  the batch Job is keyed by (connection, D), so two schedules firing at
  different moments for the same D would find the first one's batch already
  finished (#101 S2).
* :func:`observation_read_plan` -- the query instances a generation reads
  each target day: the instances of the Scale read contract it was built
  from (``app.meta.scale_read_contracts``), or, for any other ``daily``
  generation, the one 14-day daily read #107 introduced.
* :func:`retire_contract_generation` -- ``active -> retired``, retiring the
  generation's schedule in the same transaction.
* :func:`parse_daily_cron`, :func:`next_fire_after`,
  :func:`latest_fire_at_or_before`, :func:`observation_target_date` -- the
  only cron form accepted is the daily ``M H * * *`` (#101 Q5), computed in
  the generation's time zone without a cron dependency.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from dataclasses import dataclass
from typing import Any

from ..adapters.persistence.models import new_id, utcnow
from .models import (
    MetaConnectionRecord,
    MetaObservationSyncScheduleRecord,
    ObservationContractGenerationRecord,
    ObservationCoverageLedgerRecord,
    ScenarioVersionRecord,
)
from .scale_read_contracts import (
    BREAKDOWN_CONTRACT_TYPES,
    DELIVERY_CONFIGURATION,
    EXACT_WINDOW,
    INSIGHTS_READ,
    SCALE_READ_POLICY,
    AccountBinding,
    QueryInstance,
    ScaleReadContract,
    ScaleReadContractError,
    resolve_generation_contract,
    sandbox_account_binding,
)

__all__ = [
    "ContractError",
    "DAILY_CONTRACT_TYPE",
    "DEFAULT_SYNC_CRON",
    "SCHEDULED_CONTRACT_TYPES",
    "ReadPlan",
    "create_contract_generation",
    "ensure_observation_schedule",
    "observation_read_plan",
    "sandbox_account_for_connection",
    "retire_contract_generation",
    "record_coverage",
    "calculate_tenure",
    "parse_daily_cron",
    "next_fire_after",
    "latest_fire_at_or_before",
    "observation_target_date",
]

#: The daily metric contract (Scale ``MetricContractType.DAILY``): ad-level
#: insights per local day over a 14-day lookback.
DAILY_CONTRACT_TYPE = "daily"
#: Contract types the scheduled executor reads (#101 S2): the daily, exact
#: window and delivery configuration contracts, plus the breakdown families
#: the read policy declares -- none in the first batch. Which generation of
#: these types is read is decided by :func:`observation_read_plan`.
SCHEDULED_CONTRACT_TYPES = frozenset({
    DAILY_CONTRACT_TYPE, EXACT_WINDOW, DELIVERY_CONFIGURATION, *SCALE_READ_POLICY.breakdown_contract_types,
})
#: The one read of a ``daily`` generation not built from a Scale read
#: contract (#107): ad-level insights, one row per local day, D-13..D.
GENERIC_DAILY_INSTANCE = QueryInstance("daily", INSIGHTS_READ, -13, 0, "1")
#: Modes whose new generations are scheduled automatically (#101 §6).
AUTO_SCHEDULED_MODES = frozenset({"customer_sandbox"})
#: 04:00 in the account's time zone, once a day (#101 Q3): the local day is
#: over, Meta has had a few hours to settle it, and it avoids the 02:00-03:00
#: daylight-saving jump.
DEFAULT_SYNC_CRON = "0 4 * * *"

_DAILY_CRON = re.compile(r"([0-9]{1,2}) ([0-9]{1,2}) \* \* \*")
_SCHEDULE_OPEN_STATES = ("active", "paused")


class ContractError(ValueError):
    """Raised for observation contract generation invariant violations."""

    def __init__(self, message: str = "", *, code: str = "meta_contract_invalid") -> None:
        super().__init__(message)
        self.code = code


def _contract_hash(
    *,
    ad_account_id: str,
    fields: list,
    attribution_window: dict,
    breakdowns: list,
    time_window: dict,
    delivery_config: dict | None,
) -> str:
    """SHA-256 of the canonical contract definition (spec §6.1).

    Detects configuration drift that requires a new generation. It is
    deliberately not a uniqueness key -- reverted configurations may repeat
    a hash across generations; ``generation_number`` provides uniqueness.
    """
    canonical = json.dumps(
        {
            "ad_account_id": ad_account_id,
            "fields": fields,
            "attribution_window": attribution_window,
            "breakdowns": breakdowns,
            "time_window": time_window,
            "delivery_config": delivery_config,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _advisory_lock_key(workspace_id: str, meta_mode: str, connection_id: str, contract_type: str) -> int:
    """Deterministic signed-bigint key for ``pg_advisory_xact_lock``."""
    scope = f"{workspace_id}:{meta_mode}:{connection_id}:{contract_type}"
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
    return int(digest, 16) % (2**63 - 1)


def create_contract_generation(
    session: Session,
    *,
    workspace_id: str,
    meta_mode: str,
    connection_id: str,
    contract_type: str,
    ad_account_id: str,
    fields: list,
    attribution_window: dict,
    breakdowns: list,
    time_window: dict,
    local_timezone: str,
    delivery_config: dict | None = None,
) -> ObservationContractGenerationRecord:
    """Create a new active ``ObservationContractGenerationRecord``.

    Raises :class:`ContractError` if an active generation already exists
    for this ``(workspace_id, meta_mode, connection_id, contract_type)``
    scope -- generations are retired explicitly (elsewhere), never
    implicitly replaced. ``generation_number`` is one past the highest
    number ever issued in this scope (active or retired), so retirement and
    re-creation never reuses a number.
    """
    if session.get_bind().dialect.name == "postgresql":
        # §6.7: no row exists yet for the first generation in this scope,
        # so a transaction-scoped advisory lock -- not `FOR UPDATE` --
        # serializes concurrent "create the first generation" attempts.
        # Released automatically at transaction commit/rollback.
        lock_key = _advisory_lock_key(workspace_id, meta_mode, connection_id, contract_type)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

    existing_active = session.scalar(
        select(ObservationContractGenerationRecord).where(
            ObservationContractGenerationRecord.workspace_id == workspace_id,
            ObservationContractGenerationRecord.meta_mode == meta_mode,
            ObservationContractGenerationRecord.meta_connection_id == connection_id,
            ObservationContractGenerationRecord.contract_type == contract_type,
            ObservationContractGenerationRecord.state == "active",
        )
    )
    if existing_active is not None:
        raise ContractError(
            code="meta_contract_already_exists",
            message="active generation already exists for "
            f"workspace_id={workspace_id!r} meta_mode={meta_mode!r} "
            f"connection_id={connection_id!r} contract_type={contract_type!r}"
        )

    max_generation_number = session.scalar(
        select(func.max(ObservationContractGenerationRecord.generation_number)).where(
            ObservationContractGenerationRecord.workspace_id == workspace_id,
            ObservationContractGenerationRecord.meta_mode == meta_mode,
            ObservationContractGenerationRecord.meta_connection_id == connection_id,
            ObservationContractGenerationRecord.contract_type == contract_type,
        )
    )
    generation_number = (max_generation_number or 0) + 1

    generation = ObservationContractGenerationRecord(
        id=new_id("ocg"),
        workspace_id=workspace_id,
        meta_mode=meta_mode,
        meta_connection_id=connection_id,
        contract_type=contract_type,
        generation_number=generation_number,
        ad_account_id=ad_account_id,
        fields=fields,
        attribution_window=attribution_window,
        breakdowns=breakdowns,
        time_window=time_window,
        delivery_config=delivery_config,
        contract_hash=_contract_hash(
            ad_account_id=ad_account_id,
            fields=fields,
            attribution_window=attribution_window,
            breakdowns=breakdowns,
            time_window=time_window,
            delivery_config=delivery_config,
        ),
        local_timezone=local_timezone,
        state="active",
        clock_started_at=None,
    )
    session.add(generation)
    session.flush()
    if meta_mode in AUTO_SCHEDULED_MODES and _read_plan_or_raise(generation) is not None:
        ensure_observation_schedule(session, generation)
    return generation


# ---------------------------------------------------------------------------
# Read plan (#101 S2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadPlan:
    """What the scheduled executor reads for a generation each target day.

    ``contract`` and ``binding`` are set for a generation built from a Scale
    read contract and ``None`` for a generic daily generation (#107).
    """

    instances: tuple[QueryInstance, ...]
    fields: tuple[str, ...]
    attribution: dict[str, Any]
    breakdowns: tuple[str, ...]
    contract: ScaleReadContract | None = None
    binding: AccountBinding | None = None

    def instance(self, name: str | None) -> QueryInstance:
        """The named instance; ``None`` is a #107 occurrence without one."""
        if name is None:
            if len(self.instances) != 1:
                raise ContractError("an occurrence without an instance needs a one-instance plan",
                                    code="meta_read_instance_unknown")
            return self.instances[0]
        for instance in self.instances:
            if instance.name == name:
                return instance
        raise ContractError(f"no query instance {name!r} in this plan", code="meta_read_instance_unknown")


def observation_read_plan(generation: ObservationContractGenerationRecord) -> ReadPlan | None:
    """The generation's read plan, or ``None`` when no executor reads it.

    Raises :class:`app.meta.scale_read_contracts.ScaleReadContractError` when
    the generation names a Scale read contract it no longer matches: it is
    not read with a field dropped or a window moved (contract §6.2).
    Breakdown contracts outside the read policy are defined but not read.
    """
    resolved = resolve_generation_contract(
        contract_type=generation.contract_type,
        ad_account_id=generation.ad_account_id,
        fields=generation.fields,
        attribution_window=generation.attribution_window,
        breakdowns=generation.breakdowns,
        time_window=generation.time_window,
        delivery_config=generation.delivery_config,
        local_timezone=generation.local_timezone,
    )
    if resolved is not None:
        contract, binding = resolved
        if (contract.contract_type in BREAKDOWN_CONTRACT_TYPES
                and contract.contract_type not in SCALE_READ_POLICY.breakdown_contract_types):
            return None
        return ReadPlan(
            instances=contract.instances,
            fields=tuple(sorted(contract.fields)),
            attribution=contract.attribution(),
            breakdowns=contract.breakdowns,
            contract=contract,
            binding=binding,
        )
    if generation.contract_type == DAILY_CONTRACT_TYPE:
        return ReadPlan(
            instances=(GENERIC_DAILY_INSTANCE,),
            fields=tuple(sorted(generation.fields or [])),
            attribution=dict(generation.attribution_window or {}),
            breakdowns=tuple(generation.breakdowns or []),
        )
    return None


def _read_plan_or_raise(generation: ObservationContractGenerationRecord) -> ReadPlan | None:
    try:
        return observation_read_plan(generation)
    except ScaleReadContractError as exc:
        raise ContractError(str(exc), code=exc.code) from exc


def sandbox_account_for_connection(
    session: Session, *, workspace_id: str, meta_mode: str, connection_id: str,
) -> AccountBinding:
    """The account a sandbox connection's default scenario simulates."""
    connection = session.scalar(
        select(MetaConnectionRecord).where(
            MetaConnectionRecord.workspace_id == workspace_id,
            MetaConnectionRecord.meta_mode == meta_mode,
            MetaConnectionRecord.id == connection_id,
        )
    )
    if connection is None:
        raise ContractError(f"no connection {connection_id!r}", code="connection_not_found")
    scenario = session.scalar(
        select(ScenarioVersionRecord).where(
            ScenarioVersionRecord.workspace_id == workspace_id,
            ScenarioVersionRecord.meta_mode == meta_mode,
            ScenarioVersionRecord.meta_connection_id == connection_id,
            ScenarioVersionRecord.is_default == True,  # noqa: E712
        )
    )
    return sandbox_account_binding(connection.ad_account_id, scenario.scenario_config if scenario else None)


# ---------------------------------------------------------------------------
# Daily cron (#101 Q5): ``M H * * *`` only, in the generation's time zone.
# ---------------------------------------------------------------------------


def parse_daily_cron(expression: str) -> tuple[int, int]:
    """``(minute, hour)`` of a daily ``M H * * *`` expression, or raise."""
    match = _DAILY_CRON.fullmatch(expression or "")
    minute, hour = (int(match[1]), int(match[2])) if match else (-1, -1)
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        raise ContractError(
            f"only a daily 'M H * * *' cron expression is supported, not {expression!r}",
            code="meta_schedule_cron_invalid",
        )
    return minute, hour


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ContractError(
            f"unknown time zone {timezone_name!r}", code="meta_contract_timezone_invalid",
        ) from exc


def _as_utc(moment: datetime) -> datetime:
    """SQLite hands back naive values for timezone-aware columns; they are UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _daily_slot(day: date, minute: int, hour: int, zone: ZoneInfo) -> datetime:
    # A wall time the daylight-saving jump skips resolves with the offset in
    # force before it (fold=0), i.e. an hour later on the wall clock; a
    # repeated one takes its first occurrence. 04:00 is neither anywhere the
    # IANA database currently jumps.
    return datetime.combine(day, time(hour, minute), tzinfo=zone).astimezone(timezone.utc)


def next_fire_after(expression: str, timezone_name: str, after: datetime) -> datetime:
    """The first daily slot strictly after ``after``, as UTC."""
    minute, hour = parse_daily_cron(expression)
    zone = _zone(timezone_name)
    after = _as_utc(after)
    day = after.astimezone(zone).date()
    for offset in range(3):
        candidate = _daily_slot(day + timedelta(days=offset), minute, hour, zone)
        if candidate > after:
            return candidate
    raise AssertionError("a daily slot always follows within two days")  # pragma: no cover


def latest_fire_at_or_before(expression: str, timezone_name: str, moment: datetime) -> datetime:
    """The most recent daily slot at or before ``moment``, as UTC."""
    minute, hour = parse_daily_cron(expression)
    zone = _zone(timezone_name)
    moment = _as_utc(moment)
    day = moment.astimezone(zone).date()
    for offset in range(3):
        candidate = _daily_slot(day - timedelta(days=offset), minute, hour, zone)
        if candidate <= moment:
            return candidate
    raise AssertionError("a daily slot always precedes within two days")  # pragma: no cover


def observation_target_date(fire_at: datetime, timezone_name: str) -> date:
    """Target day D of a firing: the slot's local date minus one (#101 S1).

    Derived from the persisted fire time, never from the clock, so a tick
    that is retried hours later still observes the same day.
    """
    return _as_utc(fire_at).astimezone(_zone(timezone_name)).date() - timedelta(days=1)


# ---------------------------------------------------------------------------
# Schedule lifecycle (spec §3.14, #107)
# ---------------------------------------------------------------------------


def ensure_observation_schedule(
    session: Session,
    generation: ObservationContractGenerationRecord,
    *,
    cron_expression: str = DEFAULT_SYNC_CRON,
    now: datetime | None = None,
) -> MetaObservationSyncScheduleRecord:
    """The generation's open (active or paused) schedule, created if missing.

    Idempotent per generation. A new schedule is ``active`` and fires next
    at the first ``cron_expression`` slot after ``now`` in the generation's
    frozen time zone. ``workspace_id``, ``meta_mode`` and the connection come
    from the generation, so a schedule cannot disagree with it.
    """
    existing = session.scalar(
        select(MetaObservationSyncScheduleRecord)
        .where(
            MetaObservationSyncScheduleRecord.workspace_id == generation.workspace_id,
            MetaObservationSyncScheduleRecord.meta_mode == generation.meta_mode,
            MetaObservationSyncScheduleRecord.contract_generation_id == generation.id,
            MetaObservationSyncScheduleRecord.state.in_(_SCHEDULE_OPEN_STATES),
        )
        .order_by(MetaObservationSyncScheduleRecord.created_at, MetaObservationSyncScheduleRecord.id)
        .limit(1)
    )
    if existing is not None:
        return existing
    if generation.state != "active":
        raise ContractError(
            f"contract generation {generation.id!r} is {generation.state!r}",
            code="meta_contract_not_active",
        )
    if _read_plan_or_raise(generation) is None:
        raise ContractError(
            f"contract type {generation.contract_type!r} has no scheduled executor",
            code="meta_schedule_contract_unsupported",
        )
    parse_daily_cron(cron_expression)
    _require_connection_slot(session, generation, cron_expression)
    schedule = MetaObservationSyncScheduleRecord(
        id=new_id("msched"),
        workspace_id=generation.workspace_id,
        meta_mode=generation.meta_mode,
        meta_connection_id=generation.meta_connection_id,
        contract_generation_id=generation.id,
        cron_expression=cron_expression,
        state="active",
        next_fire_at=next_fire_after(cron_expression, generation.local_timezone, now or utcnow()),
    )
    session.add(schedule)
    session.flush()
    return schedule


def _require_connection_slot(
    session: Session, generation: ObservationContractGenerationRecord, cron_expression: str,
) -> None:
    """Every open schedule of a connection fires at one cron in one time zone.

    The tick and the batch are keyed by (connection, D) (#107): a schedule
    firing at another moment for the same D would find the day's batch
    already finished, and its runs would never be read.
    """
    others = session.execute(
        select(MetaObservationSyncScheduleRecord.cron_expression, ObservationContractGenerationRecord.local_timezone)
        .join(
            ObservationContractGenerationRecord,
            (ObservationContractGenerationRecord.workspace_id == MetaObservationSyncScheduleRecord.workspace_id)
            & (ObservationContractGenerationRecord.meta_mode == MetaObservationSyncScheduleRecord.meta_mode)
            & (ObservationContractGenerationRecord.id == MetaObservationSyncScheduleRecord.contract_generation_id),
        )
        .where(
            MetaObservationSyncScheduleRecord.workspace_id == generation.workspace_id,
            MetaObservationSyncScheduleRecord.meta_mode == generation.meta_mode,
            MetaObservationSyncScheduleRecord.meta_connection_id == generation.meta_connection_id,
            MetaObservationSyncScheduleRecord.state.in_(_SCHEDULE_OPEN_STATES),
        )
    ).all()
    for cron, timezone_name in others:
        if (cron, timezone_name) != (cron_expression, generation.local_timezone):
            raise ContractError(
                f"connection {generation.meta_connection_id!r} already syncs at {cron!r} in {timezone_name!r}; "
                f"a schedule at {cron_expression!r} in {generation.local_timezone!r} would split its day",
                code="meta_schedule_connection_mismatch",
            )


def retire_contract_generation(
    session: Session,
    *,
    generation_id: str,
    retired_by_generation_id: str | None = None,
) -> ObservationContractGenerationRecord:
    """``active -> retired`` (spec §3.8), retiring its schedule with it.

    Idempotent: retiring a retired generation changes nothing. The schedule
    moves to ``retired`` in the same transaction, so no dispatcher can fire a
    generation that no longer collects.
    """
    generation = session.scalar(
        select(ObservationContractGenerationRecord)
        .where(ObservationContractGenerationRecord.id == generation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation is None:
        raise ContractError(f"no contract generation {generation_id!r}", code="meta_contract_not_found")
    if generation.state == "active":
        generation.state = "retired"
        generation.retired_at = utcnow()
        generation.retired_by_generation_id = retired_by_generation_id
    session.execute(
        update(MetaObservationSyncScheduleRecord)
        .where(
            MetaObservationSyncScheduleRecord.workspace_id == generation.workspace_id,
            MetaObservationSyncScheduleRecord.meta_mode == generation.meta_mode,
            MetaObservationSyncScheduleRecord.contract_generation_id == generation.id,
            MetaObservationSyncScheduleRecord.state.in_(_SCHEDULE_OPEN_STATES),
        )
        .values(state="retired")
    )
    session.flush()
    return generation


def record_coverage(
    session: Session,
    *,
    generation_id: str,
    coverage_date: date,
    sync_run_id: str,
    attestation_id: str,
) -> ObservationCoverageLedgerRecord:
    """Append one ``(contract_generation_id, coverage_date)`` ledger entry.

    ``workspace_id`` and ``meta_mode`` are read from the contract
    generation, not accepted as parameters -- a coverage entry cannot
    disagree with the generation it covers. Starts the generation's
    observation clock on the first entry ever recorded for it (§6.6.2):
    coverage begins when the first ledger entry is written, not when the
    generation row was created.
    """
    generation = session.get(ObservationContractGenerationRecord, generation_id)
    if generation is None:
        raise ContractError(f"no contract generation {generation_id!r}", code="meta_contract_not_found")

    entry = ObservationCoverageLedgerRecord(
        id=new_id("ocl"),
        workspace_id=generation.workspace_id,
        meta_mode=generation.meta_mode,
        contract_generation_id=generation.id,
        coverage_date=coverage_date,
        sync_run_id=sync_run_id,
        attestation_id=attestation_id,
    )
    session.add(entry)

    if generation.clock_started_at is None:
        generation.clock_started_at = utcnow()

    session.flush()
    return entry


def calculate_tenure(session: Session, generation_id: str) -> int:
    """Longest run of consecutive ``coverage_date`` values in the ledger (§6.6.3).

    Derived on demand from ledger entries rather than tracked as a mutable
    counter (§6.6.5), so it self-corrects when coverage entries are removed
    (e.g. after an attestation is invalidated, §6.9.5).
    """
    coverage_dates = session.scalars(
        select(ObservationCoverageLedgerRecord.coverage_date)
        .where(ObservationCoverageLedgerRecord.contract_generation_id == generation_id)
        .order_by(ObservationCoverageLedgerRecord.coverage_date)
    ).all()
    if not coverage_dates:
        return 0

    longest = current = 1
    for previous_date, current_date in zip(coverage_dates, coverage_dates[1:]):
        if current_date - previous_date == timedelta(days=1):
            current += 1
        else:
            current = 1
        longest = max(longest, current)
    return longest
