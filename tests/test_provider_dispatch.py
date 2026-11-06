"""Crash-window recovery tests for the generic ExternalCallCoordinator (#24).

Spec §3G: five durability windows are injected explicitly and must be
recoverable. This suite exercises each by driving the coordinator's phases
directly (white-box), matching the spec's "injected explicitly" language,
against an isolated in-memory SQLite database built with
``Base.metadata.create_all`` -- same pattern as ``tests/test_coordinator.py``
and ``tests/test_localization_platform.py``.
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import JobRecord, OperationRecord, WorkspaceRecord
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
# Imported for its side effect of registering `credential_versions` /
# `credential_bindings` in Base.metadata -- other already-imported models
# (e.g. datasource_credential_bindings) declare a string FK to
# `credential_versions.workspace_id` that only resolves once this module has
# been imported, exactly as `tests/test_coordinator.py` already does.
from app.entitlements.credentials import (  # noqa: F401
    CredentialBindingRecord,
    CredentialVersionRecord,
)
from app.platform.tenancy import TenantContext
from app.providers.adapter import ProviderCapabilities, ProviderDispatchError, ProviderResult
from app.providers.dispatch import (
    ChargeabilityMismatchError,
    CoordinatedCallResult,
    ExternalCallCoordinator,
    JobNotFoundError,
    ReconciliationNotApplicableError,
)

WORKSPACE_ID = "ws_dispatch_test"
OPERATION_ID = "op_dispatch_1"
JOB_ID = "job_dispatch_1"
RESERVATION_ID = "sr_dispatch_1"
CALL_KEY_HASH = "hash_dispatch_abc"
PROVIDER = "controlled_mock"
# Deliberately a step_kind with no truth_package_id requirement (unlike
# 'translation'/'source_analysis'/...): these tests exercise the coordinator's
# domain-agnostic dispatch mechanics, not Truth-package fixture wiring, and
# the whole point of §2D is that step_kind is opaque metadata to the
# coordinator regardless of its value.
STEP_KIND = "generation"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_ctx() -> TenantContext:
    return TenantContext(
        workspace_id=WORKSPACE_ID,
        role="system",
        executor="system:worker",
        initiator_id="user_test",
        initiator_role="owner",
    )


@dataclass(frozen=True)
class _Request:
    payload: str


@dataclass(frozen=True)
class _Result:
    output: str


class FakeAdapter:
    """A configurable ExternalCallAdapter test double.

    ``outcomes`` is a queue of behaviors consumed one per ``execute()``
    call, so a test can script "first call never got a response" without
    actually blocking a thread.
    """

    provider = PROVIDER

    def __init__(
        self,
        *,
        accepts_idempotency_key: bool = True,
        supports_result_lookup_by_request_id: bool = False,
        outcomes: list[str] | None = None,
    ):
        self._caps = ProviderCapabilities(
            accepts_idempotency_key=accepts_idempotency_key,
            supports_result_lookup_by_request_id=supports_result_lookup_by_request_id,
        )
        self._outcomes = outcomes if outcomes is not None else ["success"]
        self.calls: list[Any] = []
        self._lookup_store: dict[str, ProviderResult[_Result]] = {}
        self._next_request_id = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def execute(self, request, *, credential, idempotency_key) -> ProviderResult[_Result]:
        self.calls.append((request, credential, idempotency_key))
        outcome = self._outcomes.pop(0) if self._outcomes else "success"
        if outcome == "success":
            self._next_request_id += 1
            request_id = f"preq_{self._next_request_id}"
            result = ProviderResult(
                output=_Result(output=f"translated:{request.payload if request else ''}"),
                provider_request_id=request_id,
                idempotency_key_accepted=True,
            )
            self._lookup_store[request_id] = result
            return result
        if outcome == "terminal_failure":
            raise ProviderDispatchError(
                dispatch_certainty="not_dispatched", message="rejected", error_code="bad_request",
            )
        if outcome == "uncertain_failure":
            raise ProviderDispatchError(
                dispatch_certainty="uncertain", message="timeout", error_code="timeout",
            )
        raise AssertionError(f"unknown scripted outcome {outcome!r}")

    async def lookup_by_request_id(self, request_id: str) -> ProviderResult[_Result] | None:
        return self._lookup_store.get(request_id)


class FakeEvidenceRecorder:
    """Records every call it receives so tests can assert on fold/replay
    behavior without a real domain model."""

    def __init__(self) -> None:
        self.link_calls: list[Any] = []
        self.materialize_calls: list[Any] = []
        self.persist_calls: list[Any] = []

    def link_prepared_attempt(self, session, ctx, *, attempt):
        self.link_calls.append(attempt.id)

    def materialize(self, ctx, *, attempt_id, output):
        self.materialize_calls.append((attempt_id, output))
        return f"ref:{attempt_id}"

    def persist_result(self, session, ctx, *, attempt, reference, output):
        self.persist_calls.append((attempt.id, reference, output))


class DispatchCoordinatorTestBase(unittest.IsolatedAsyncioTestCase):
    """In-memory SQLite database with every table, seeded with a Workspace,
    Operation, Job and (chargeable) SpendReservation."""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self._SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

        @contextmanager
        def session_factory(ctx: TenantContext) -> Iterator[Session]:
            session = self._SessionLocal()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        self.session_factory = session_factory
        self.coordinator = ExternalCallCoordinator(session_factory=session_factory)
        self._seed_database()

    def _seed_database(self):
        now = _utcnow()
        session = self._SessionLocal()
        try:
            session.add(WorkspaceRecord(id=WORKSPACE_ID, name="Test Workspace", created_at=now))
            session.flush()

            session.add(OperationRecord(
                id=OPERATION_ID,
                workspace_id=WORKSPACE_ID,
                command_type="localization.confirm",
                state="running",
                created_at=now,
                updated_at=now,
            ))
            session.flush()

            session.add(JobRecord(
                id=JOB_ID,
                workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID,
                type="localization.translate_copy",
                payload_json={"source": "hello"},
                idempotency_key="idem_job_dispatch_1",
                state="running",
                initiator_role="owner",
                lease_owner="worker-1",
                created_at=now,
            ))
            session.flush()

            session.add(SpendReservationRecord(
                id=RESERVATION_ID,
                workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID,
                job_id=JOB_ID,
                semantic_key_hash=CALL_KEY_HASH,
                state="reserved",
                provider=PROVIDER,
                execution_mode="byok",
                provider_currency="USD",
                maximum_provider_amount=Decimal("1.00"),
                budget_currency="USD",
                maximum_budget_amount=Decimal("1.00"),
                outstanding_budget_amount=Decimal("1.00"),
                entitlement_version=1,
                lease_sequence=1,
                guardrail_revision=1,
                platform_policy_version=1,
                created_at=now,
                updated_at=now,
            ))
            session.flush()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- helpers --------------------------------------------------------

    def _attempt_row(self, attempt_id: str) -> ProviderCallAttemptRecord:
        with self._SessionLocal() as session:
            return (
                session.query(ProviderCallAttemptRecord)
                .filter_by(id=attempt_id, workspace_id=WORKSPACE_ID)
                .one()
            )

    def _count_attempts(self) -> int:
        with self._SessionLocal() as session:
            return (
                session.query(ProviderCallAttemptRecord)
                .filter_by(workspace_id=WORKSPACE_ID, job_id=JOB_ID)
                .count()
            )

    def _reservation_row(self, reservation_id: str) -> SpendReservationRecord:
        with self._SessionLocal() as session:
            return (
                session.query(SpendReservationRecord)
                .filter_by(id=reservation_id, workspace_id=WORKSPACE_ID)
                .one()
            )

    def _ledger_entries_for(self, reservation_id: str) -> list[ProviderCostLedgerEntryRecord]:
        with self._SessionLocal() as session:
            return (
                session.query(ProviderCostLedgerEntryRecord)
                .filter_by(workspace_id=WORKSPACE_ID, spend_reservation_id=reservation_id)
                .all()
            )

    def _execute_kwargs(self, **overrides) -> dict:
        defaults = dict(
            ctx=_make_ctx(),
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind=STEP_KIND,
            execution_mode="byok",
            chargeability="charged",
            provider=PROVIDER,
            spend_reservation_id=RESERVATION_ID,
        )
        defaults.update(overrides)
        return defaults


class TestExternalCallCoordinator(DispatchCoordinatorTestBase):
    # -- Window 1: Job claimed before prepare ----------------------------

    def test_window_1_job_claimed_before_prepare(self):
        """Only the Job claim is committed; no ProviderCallAttempt exists
        yet. Re-entering prepare (as a Worker restart would) must reclaim
        idempotently: exactly one attempt row, same id both times."""
        ctx = _make_ctx()
        evidence_recorder = FakeEvidenceRecorder()

        first = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind=STEP_KIND,
            execution_mode="byok",
            chargeability="charged",
            provider=PROVIDER,
            evidence_recorder=evidence_recorder,
            spend_reservation_id=RESERVATION_ID,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        self.assertFalse(first["replay"])
        self.assertEqual(self._count_attempts(), 1)
        attempt_id = first["attempt_id"]

        # Simulate a crash right here (Job claimed + attempt prepared and
        # committed) and the Worker re-entering prepare on redelivery.
        second = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind=STEP_KIND,
            execution_mode="byok",
            chargeability="charged",
            provider=PROVIDER,
            evidence_recorder=evidence_recorder,
            spend_reservation_id=RESERVATION_ID,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        self.assertFalse(second["replay"])
        self.assertEqual(second["attempt_id"], attempt_id)
        self.assertEqual(self._count_attempts(), 1, "reclaim must not create a duplicate attempt row")
        # link_prepared_attempt only fires once -- for the row that was
        # actually created, not on the idempotent reclaim.
        self.assertEqual(len(evidence_recorder.link_calls), 1)

    def test_prepare_rejects_unknown_job(self):
        ctx = _make_ctx()
        with self.assertRaises(JobNotFoundError):
            self.coordinator._prepare_in_transaction(
                ctx,
                job_id="job_does_not_exist",
                operation_id=OPERATION_ID,
                call_semantic_key_hash=CALL_KEY_HASH,
                step_kind=STEP_KIND,
                execution_mode="byok",
                chargeability="charged",
                provider=PROVIDER,
                evidence_recorder=FakeEvidenceRecorder(),
                spend_reservation_id=RESERVATION_ID,
                truth_package_id=None,
                run_item_id=None,
                model=None,
            )

    def test_prepare_rejects_chargeability_mode_mismatch(self):
        ctx = _make_ctx()
        with self.assertRaises(ChargeabilityMismatchError):
            self.coordinator._prepare_in_transaction(
                ctx,
                job_id=JOB_ID,
                operation_id=OPERATION_ID,
                call_semantic_key_hash=CALL_KEY_HASH,
                step_kind=STEP_KIND,
                execution_mode="mock",
                chargeability="charged",  # charged requires byok + a reservation
                provider=PROVIDER,
                evidence_recorder=FakeEvidenceRecorder(),
                spend_reservation_id=None,
                truth_package_id=None,
                run_item_id=None,
                model=None,
            )

    # -- Window 2: ProviderAttempt prepared before network ---------------

    async def test_window_2_prepared_before_network(self):
        """A prior run got as far as committing 'prepared' and crashed
        before ever calling the adapter. execute() must redispatch the
        same authorized attempt -- not create a second one."""
        ctx = _make_ctx()
        prep_recorder = FakeEvidenceRecorder()
        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind=STEP_KIND,
            execution_mode="byok",
            chargeability="charged",
            provider=PROVIDER,
            evidence_recorder=prep_recorder,
            spend_reservation_id=RESERVATION_ID,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        attempt_id = prepared["attempt_id"]
        self.assertEqual(self._attempt_row(attempt_id).dispatch_state, "prepared")

        adapter = FakeAdapter(outcomes=["success"])
        evidence_recorder = FakeEvidenceRecorder()
        result = await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="bonjour"),
                adapter=adapter,
                evidence_recorder=evidence_recorder,
            )
        )

        self.assertIsInstance(result, CoordinatedCallResult)
        self.assertFalse(result.replayed)
        self.assertTrue(result.success)
        self.assertEqual(result.attempt_id, attempt_id)
        self.assertEqual(len(adapter.calls), 1, "must dispatch exactly once, reusing the prepared attempt")
        self.assertEqual(self._count_attempts(), 1)
        self.assertEqual(self._attempt_row(attempt_id).dispatch_state, "succeeded")
        # idempotency key issued at prepare time is what actually reaches
        # the provider.
        self.assertEqual(adapter.calls[0][2], self._attempt_row(attempt_id).provider_idempotency_key)

    # -- Window 3: response received before completion commit ------------

    async def test_window_3_response_before_completion(self):
        """The 'dispatch_started' commit landed (dispatch_state=
        'dispatching') but the process crashed before phase 3 committed
        any result evidence. Recovery must classify this uncertain and
        must NOT call the adapter again."""
        ctx = _make_ctx()
        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind=STEP_KIND,
            execution_mode="byok",
            chargeability="charged",
            provider=PROVIDER,
            evidence_recorder=FakeEvidenceRecorder(),
            spend_reservation_id=RESERVATION_ID,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        attempt_id = prepared["attempt_id"]
        # Simulate phase 2's pre-network commit, then a crash before the
        # network call ever returns / before phase 3 commits.
        self.coordinator._mark_dispatching(ctx, attempt_id=attempt_id)
        self.assertEqual(self._attempt_row(attempt_id).dispatch_state, "dispatching")

        adapter = FakeAdapter(outcomes=["success"])
        evidence_recorder = FakeEvidenceRecorder()
        result = await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="hola"),
                adapter=adapter,
                evidence_recorder=evidence_recorder,
            )
        )

        self.assertTrue(result.replayed)
        self.assertFalse(result.success)
        self.assertEqual(result.dispatch_certainty, "uncertain")
        self.assertEqual(len(adapter.calls), 0, "must never blindly redispatch mid-flight")
        self.assertEqual(self._attempt_row(attempt_id).dispatch_state, "uncertain")

    # -- Window 4: completion committed before domain fold ----------------

    async def test_window_4_completion_before_fold(self):
        """A first execute() call fully succeeded and committed. A second
        execute() call for the same job (e.g. a redelivered Job message,
        or the domain fold retrying after its own crash) must replay the
        stored outcome without calling the provider or materializing
        again -- no duplicate cost, no duplicate domain evidence."""
        adapter_1 = FakeAdapter(outcomes=["success"])
        recorder_1 = FakeEvidenceRecorder()
        first = await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="guten tag"),
                adapter=adapter_1,
                evidence_recorder=recorder_1,
            )
        )
        self.assertTrue(first.success)
        self.assertFalse(first.replayed)
        self.assertEqual(len(adapter_1.calls), 1)
        self.assertEqual(len(recorder_1.persist_calls), 1)

        adapter_2 = FakeAdapter(outcomes=["success"])
        recorder_2 = FakeEvidenceRecorder()
        second = await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="guten tag"),
                adapter=adapter_2,
                evidence_recorder=recorder_2,
            )
        )

        self.assertTrue(second.replayed)
        self.assertTrue(second.success)
        self.assertEqual(second.attempt_id, first.attempt_id)
        self.assertEqual(second.evidence_reference, first.evidence_reference)
        self.assertEqual(len(adapter_2.calls), 0, "replay must never call the provider again")
        self.assertEqual(len(recorder_2.materialize_calls), 0)
        self.assertEqual(len(recorder_2.persist_calls), 0)
        self.assertEqual(self._count_attempts(), 1)

    # -- Window 5: provider may have received the request, no response ---

    async def test_window_5_request_sent_no_response(self):
        """An 'uncertain' attempt (window 3's outcome) is reconciled --
        never blindly retried by execute() -- through the explicit
        reconcile() seam, and reconciliation is capability-aware."""
        ctx = _make_ctx()

        # --- Sub-case A: no lookup, no idempotency guarantee -> stays uncertain.
        prepared_a = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind=STEP_KIND,
            execution_mode="byok",
            chargeability="charged",
            provider=PROVIDER,
            evidence_recorder=FakeEvidenceRecorder(),
            spend_reservation_id=RESERVATION_ID,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        attempt_id_a = prepared_a["attempt_id"]
        self.coordinator._mark_dispatching(ctx, attempt_id=attempt_id_a)
        # Recovery re-entry classifies it uncertain (window 3 path).
        await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="never used"),
                adapter=FakeAdapter(outcomes=["success"]),
                evidence_recorder=FakeEvidenceRecorder(),
            )
        )
        self.assertEqual(self._attempt_row(attempt_id_a).dispatch_state, "uncertain")

        no_guarantee_adapter = FakeAdapter(
            accepts_idempotency_key=False,
            supports_result_lookup_by_request_id=False,
            outcomes=["success"],
        )
        recorder_a = FakeEvidenceRecorder()
        reconciled_a = await self.coordinator.reconcile(
            ctx,
            attempt_id=attempt_id_a,
            adapter=no_guarantee_adapter,
            evidence_recorder=recorder_a,
            request=_Request(payload="never used"),
        )
        self.assertEqual(reconciled_a.dispatch_certainty, "uncertain")
        self.assertFalse(reconciled_a.success)
        self.assertEqual(len(no_guarantee_adapter.calls), 0, "must never blindly retry without a capability guarantee")
        self.assertEqual(self._attempt_row(attempt_id_a).dispatch_state, "uncertain")

        # --- Sub-case B: idempotency guaranteed -> safe to retry with the same key.
        job_id_b = "job_dispatch_2"
        with self._SessionLocal() as session:
            session.add(JobRecord(
                id=job_id_b,
                workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID,
                type="localization.translate_copy",
                payload_json={},
                idempotency_key="idem_job_dispatch_2",
                state="running",
                initiator_role="owner",
                lease_owner="worker-1",
                created_at=_utcnow(),
            ))
            session.add(SpendReservationRecord(
                id="sr_dispatch_2",
                workspace_id=WORKSPACE_ID,
                operation_id=OPERATION_ID,
                job_id=job_id_b,
                semantic_key_hash="hash_dispatch_xyz",
                state="reserved",
                provider=PROVIDER,
                execution_mode="byok",
                provider_currency="USD",
                maximum_provider_amount=Decimal("1.00"),
                budget_currency="USD",
                maximum_budget_amount=Decimal("1.00"),
                outstanding_budget_amount=Decimal("1.00"),
                entitlement_version=1,
                lease_sequence=1,
                guardrail_revision=1,
                platform_policy_version=1,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            ))
            session.commit()

        prepared_b = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=job_id_b,
            operation_id=OPERATION_ID,
            call_semantic_key_hash="hash_dispatch_xyz",
            step_kind=STEP_KIND,
            execution_mode="byok",
            chargeability="charged",
            provider=PROVIDER,
            evidence_recorder=FakeEvidenceRecorder(),
            spend_reservation_id="sr_dispatch_2",
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        attempt_id_b = prepared_b["attempt_id"]
        self.coordinator._mark_dispatching(ctx, attempt_id=attempt_id_b)
        await self.coordinator.execute(
            **self._execute_kwargs(
                job_id=job_id_b,
                call_semantic_key_hash="hash_dispatch_xyz",
                spend_reservation_id="sr_dispatch_2",
                request=_Request(payload="never used"),
                adapter=FakeAdapter(outcomes=["success"]),
                evidence_recorder=FakeEvidenceRecorder(),
            )
        )
        self.assertEqual(self._attempt_row(attempt_id_b).dispatch_state, "uncertain")

        idempotent_adapter = FakeAdapter(
            accepts_idempotency_key=True,
            supports_result_lookup_by_request_id=False,
            outcomes=["success"],
        )
        recorder_b = FakeEvidenceRecorder()
        reconciled_b = await self.coordinator.reconcile(
            ctx,
            attempt_id=attempt_id_b,
            adapter=idempotent_adapter,
            evidence_recorder=recorder_b,
            request=_Request(payload="hallo"),
        )
        self.assertTrue(reconciled_b.success)
        self.assertEqual(reconciled_b.dispatch_certainty, "dispatched")
        self.assertEqual(len(idempotent_adapter.calls), 1)
        self.assertEqual(self._attempt_row(attempt_id_b).dispatch_state, "succeeded")
        self.assertEqual(len(recorder_b.persist_calls), 1)

    def test_reconcile_rejects_non_uncertain_attempt(self):
        ctx = _make_ctx()
        prepared = self.coordinator._prepare_in_transaction(
            ctx,
            job_id=JOB_ID,
            operation_id=OPERATION_ID,
            call_semantic_key_hash=CALL_KEY_HASH,
            step_kind=STEP_KIND,
            execution_mode="byok",
            chargeability="charged",
            provider=PROVIDER,
            evidence_recorder=FakeEvidenceRecorder(),
            spend_reservation_id=RESERVATION_ID,
            truth_package_id=None,
            run_item_id=None,
            model=None,
        )
        with self.assertRaises(ReconciliationNotApplicableError):
            import asyncio
            asyncio.run(self.coordinator.reconcile(
                ctx,
                attempt_id=prepared["attempt_id"],
                adapter=FakeAdapter(),
                evidence_recorder=FakeEvidenceRecorder(),
            ))

    # -- Reservation settlement / release (fix round 1, Important #1/#2) --

    async def test_successful_completion_settles_reservation_and_writes_ledger(self):
        """On a successful dispatch, _complete_in_transaction must settle
        the chargeable reservation, zero its outstanding budget, stamp
        settled_at, and append exactly one ProviderCostLedgerEntryRecord --
        mirroring app.entitlements.coordinator's completion phase."""
        before = self._reservation_row(RESERVATION_ID)
        self.assertEqual(before.state, "reserved")

        result = await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="settle me"),
                adapter=FakeAdapter(outcomes=["success"]),
                evidence_recorder=FakeEvidenceRecorder(),
            )
        )
        self.assertTrue(result.success)

        reservation = self._reservation_row(RESERVATION_ID)
        self.assertEqual(reservation.state, "settled")
        self.assertEqual(reservation.outstanding_budget_amount, Decimal("0"))
        self.assertIsNotNone(reservation.settled_at)
        self.assertIsNone(reservation.released_at)

        ledger_entries = self._ledger_entries_for(RESERVATION_ID)
        self.assertEqual(len(ledger_entries), 1)
        entry = ledger_entries[0]
        self.assertEqual(entry.entry_type, "estimated_settlement")
        self.assertEqual(entry.direction, "increase")
        self.assertEqual(entry.provider, PROVIDER)
        self.assertEqual(entry.semantic_key_hash, CALL_KEY_HASH)
        self.assertEqual(entry.provider_amount, reservation.maximum_provider_amount)
        self.assertEqual(entry.budget_amount, reservation.maximum_budget_amount)

        # completed_at (Important #2) is populated on the terminal attempt.
        attempt = self._attempt_row(result.attempt_id)
        self.assertIsNotNone(attempt.completed_at)

    async def test_terminal_failure_releases_reservation_without_ledger_entry(self):
        """A terminal (not_dispatched) failure releases the reservation --
        failed_no_charge -- and must never write a cost-ledger entry."""
        result = await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="fail me"),
                adapter=FakeAdapter(outcomes=["terminal_failure"]),
                evidence_recorder=FakeEvidenceRecorder(),
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(result.dispatch_certainty, "not_dispatched")

        reservation = self._reservation_row(RESERVATION_ID)
        self.assertEqual(reservation.state, "released")
        self.assertEqual(reservation.outstanding_budget_amount, Decimal("0"))
        self.assertIsNotNone(reservation.released_at)
        self.assertIsNone(reservation.settled_at)

        self.assertEqual(self._ledger_entries_for(RESERVATION_ID), [])

        attempt = self._attempt_row(result.attempt_id)
        self.assertEqual(attempt.dispatch_state, "failed")
        self.assertIsNotNone(attempt.completed_at)

    async def test_uncertain_completion_leaves_reservation_untouched(self):
        """An uncertain outcome (from _complete_in_transaction, not the
        prepare-phase window-3 path) must not settle or release the
        reservation -- reconcile() is what eventually resolves it -- but
        completed_at is still stamped on the attempt."""
        result = await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="uncertain me"),
                adapter=FakeAdapter(outcomes=["uncertain_failure"]),
                evidence_recorder=FakeEvidenceRecorder(),
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(result.dispatch_certainty, "uncertain")

        reservation = self._reservation_row(RESERVATION_ID)
        self.assertEqual(reservation.state, "reserved")
        self.assertEqual(reservation.outstanding_budget_amount, Decimal("1.00"))
        self.assertIsNone(reservation.settled_at)
        self.assertIsNone(reservation.released_at)
        self.assertEqual(self._ledger_entries_for(RESERVATION_ID), [])

        attempt = self._attempt_row(result.attempt_id)
        self.assertEqual(attempt.dispatch_state, "uncertain")
        self.assertIsNotNone(attempt.completed_at)

    async def test_reconcile_success_settles_reservation_left_open_by_earlier_uncertainty(self):
        """A reservation left 'reserved' by an earlier uncertain completion
        is settled once reconcile() resolves the attempt to success."""
        ctx = _make_ctx()
        first = await self.coordinator.execute(
            **self._execute_kwargs(
                request=_Request(payload="first"),
                adapter=FakeAdapter(outcomes=["uncertain_failure"]),
                evidence_recorder=FakeEvidenceRecorder(),
            )
        )
        self.assertEqual(self._reservation_row(RESERVATION_ID).state, "reserved")

        reconciled = await self.coordinator.reconcile(
            ctx,
            attempt_id=first.attempt_id,
            adapter=FakeAdapter(accepts_idempotency_key=True, outcomes=["success"]),
            evidence_recorder=FakeEvidenceRecorder(),
            request=_Request(payload="first"),
        )
        self.assertTrue(reconciled.success)

        reservation = self._reservation_row(RESERVATION_ID)
        self.assertEqual(reservation.state, "settled")
        self.assertEqual(len(self._ledger_entries_for(RESERVATION_ID)), 1)


if __name__ == "__main__":
    unittest.main()
