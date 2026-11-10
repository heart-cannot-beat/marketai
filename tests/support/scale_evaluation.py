"""One evaluable Scale run, for tests that drive the Worker path (#96, #97).

The rows are ``tests.test_scale_orchestration.TestFullEvaluationHappyPath``'s:
an active Enrollment on a collecting Episode, a published Policy (no Target),
a three-member metric-contract set, a ``complete`` source manifest covering
21 daily days, three exact windows and one delivery configuration, and one
``requested`` run. The delivery source carries the Episode's own
comparability hash, so the run evaluates instead of rolling the Episode over
(``tests/test_scale_episode_rollover_pg.py`` covers the rollover).

``seed_active_thread`` then opens an Advice thread on the evaluated
Episode, the way Phase C's Advice processing does: the happy-path
evaluation itself produces no Advice candidate. With
``delivery_anomaly=True`` the last two days deliver nothing, the evaluator
itself reports a delivery anomaly, and Phase C opens an
``investigate_delivery_anomaly`` thread (#111). ``seed_later_run`` adds a
second live run on the same manifest with a later freshness reference: the
next evaluation, which moves every active thread's deadline.

Every id is new, so the rows can stay in a shared database: the Scale
evidence tables refuse DELETE. The caller owns the transaction and has
already provisioned the Workspace.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.persistence.models import new_id
from app.platform.tenancy import TenantContext, system_context
from app.scale.advice import process_advice_candidates
from app.scale.models import (
    ScaleEvaluationRunRecord,
    ScaleMetricSemanticContractSetMemberRecord,
    ScaleMetricSemanticContractSetRecord,
)
from tests.test_scale_advice import _candidate, _candidate_row
from tests.test_scale_evidence import (
    _daily_source,
    _delivery_source,
    _enrollment,
    _episode,
    _exact_source,
    _item,
    _manifest,
    _policy,
    _run,
)

MODE = "internal_validation"
#: The connection of the seeded Enrollment and Episode (``tests.test_scale_evidence``).
CONNECTION_ID = "mcon_1"
AS_OF = date(2026, 9, 5)
#: The run's freshness reference. The Policy's 24-hour freshness lag put its
#: Advice deadline at 2026-09-07 04:00 UTC, long past.
FRESHNESS_REFERENCE_AT = datetime(2026, 9, 6, 4, 0, tzinfo=timezone.utc)
#: The seeded Policy's ``freshness_floor_maximum_lag_hours``.
FRESHNESS_LAG = timedelta(hours=24)
#: The last days of the current window that deliver nothing under
#: ``delivery_anomaly=True``: two such days make the evaluator report a
#: delivery anomaly (one does not; seven close the Episode instead).
ANOMALY_DAYS = (AS_OF - timedelta(days=1), AS_OF)


@dataclass(frozen=True)
class SeededRun:
    workspace_id: str
    meta_mode: str
    enrollment_id: str
    episode_id: str
    policy_id: str
    run_id: str


def worker_context(workspace_id: str, meta_mode: str | None = MODE) -> TenantContext:
    """A claimed Job's context (``jobs.claim_next``), with the given Meta mode."""
    return replace(system_context(workspace_id, initiator_id="user_1"), meta_mode=meta_mode)


def seed_evaluable_run(
    session: Session,
    workspace_id: str,
    *,
    meta_mode: str = MODE,
    delivery_anomaly: bool = False,
    freshness_reference_at: datetime = FRESHNESS_REFERENCE_AT,
) -> SeededRun:
    current_start = AS_OF - timedelta(days=6)
    current_end = AS_OF + timedelta(days=1)
    baseline_start = current_start - timedelta(days=7)

    enrollment = _enrollment(workspace_id, meta_mode)
    contract_set = ScaleMetricSemanticContractSetRecord(
        id=new_id("smcs"), workspace_id=workspace_id, meta_mode=meta_mode, created_by="user_1")
    session.add_all([enrollment, contract_set])
    session.flush()
    session.add_all([
        ScaleMetricSemanticContractSetMemberRecord(
            id=new_id("smcm"), contract_set_id=contract_set.id, contract_type=kind,
            observation_contract_generation="gen-1", contract_hash=f"{kind}-hash")
        for kind in ("daily", "exact_window", "delivery_configuration")
    ])
    episode = _episode(workspace_id, meta_mode, enrollment.id, contract_set.id)
    policy = _policy()
    session.add_all([episode, policy])
    session.flush()
    enrollment.current_episode_id = episode.id
    session.flush()

    days = [baseline_start + timedelta(days=offset) for offset in range((current_end - baseline_start).days)]
    daily = {day: _daily_source(workspace_id, meta_mode, episode.id, day) for day in days}
    if delivery_anomaly:
        for day in ANOMALY_DAYS:
            row = daily[day]
            row.delivered = False
            row.spend = row.purchase_value = Decimal("0")
            row.impressions = row.link_clicks = row.purchases = 0
    exact = {role: _exact_source(workspace_id, meta_mode, episode.id, role, start, end)
             for role, start, end in (("current_7d", current_start, current_end),
                                      ("baseline_7d", baseline_start, current_start),
                                      ("rolling_14d", baseline_start, current_end))}
    delivery = _delivery_source(workspace_id, meta_mode, episode.id)
    assert delivery.comparability_hash == episode.delivery_comparability_hash  # no rollover
    session.add_all([*daily.values(), *exact.values(), delivery])
    session.flush()
    manifest = _manifest(workspace_id, meta_mode, episode.id, AS_OF)
    session.add(manifest)
    session.flush()
    session.add_all([
        *(_item(workspace_id, meta_mode, manifest.id, "daily", row.id, local_day=day) for day, row in daily.items()),
        *(_item(workspace_id, meta_mode, manifest.id, "exact_window", row.id, window_role=role)
          for role, row in exact.items()),
        _item(workspace_id, meta_mode, manifest.id, "delivery_configuration", delivery.id),
    ])
    session.flush()
    run = _run(workspace_id, meta_mode, episode.id, enrollment.id, manifest, AS_OF, policy_id=policy.id)
    run.freshness_reference_at = freshness_reference_at
    session.add(run)
    session.flush()
    return SeededRun(
        workspace_id=workspace_id, meta_mode=meta_mode, enrollment_id=enrollment.id,
        episode_id=episode.id, policy_id=policy.id, run_id=run.id,
    )


def seed_later_run(session: Session, seeded: SeededRun, freshness_reference_at: datetime) -> str:
    """A second ``requested`` live run on the seeded run's manifest, one
    semantic event later, with a later freshness reference: once evaluated,
    it is the Episode's freshest evidence and moves each active thread's
    deadline to ``freshness_reference_at`` plus the Policy's lag."""
    first = session.get(ScaleEvaluationRunRecord, seeded.run_id)
    run = ScaleEvaluationRunRecord(
        id=new_id("sevr"),
        freshness_reference_at=freshness_reference_at,
        semantic_event_sequence=first.semantic_event_sequence + 1,
        run_state="requested",
        **{key: getattr(first, key) for key in (
            "workspace_id", "meta_mode", "meta_connection_id", "scenario_scope_key", "episode_id",
            "enrollment_id", "target_local_day", "trigger_kind", "source_manifest_id",
            "source_manifest_hash", "source_revision", "policy_version_id", "target_version_id",
            "normalized_target_key", "algorithm_version", "replay_generation",
            "original_initiator_id", "cancellation_generation")},
    )
    session.add(run)
    session.flush()
    return run.id


def seed_active_thread(
    session: Session, context: TenantContext, seeded: SeededRun, evaluation_id: str,
) -> str:
    """Persist one Advice candidate on ``evaluation_id`` and resolve it into an
    active thread through ``process_advice_candidates``, as Phase C step 6
    would. Its deadline is the evaluated run's freshness reference plus the
    Policy's 24 hours."""
    candidate = _candidate(reason_signature=f"rs-{new_id('ci')}")
    session.add(_candidate_row(seeded.workspace_id, seeded.meta_mode, evaluation_id, candidate))
    session.flush()
    [thread_id] = process_advice_candidates(
        session, context, seeded.episode_id, evaluation_id, [candidate],
        trigger_kind="live", evaluated_local_day=AS_OF, projection_eligible=False,
        occurred_at=FRESHNESS_REFERENCE_AT,
    )
    return thread_id
