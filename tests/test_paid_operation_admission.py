"""Queue-publication and legacy recovery tests for paid admission."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import JobRecord, OperationRecord, WorkspaceRecord
from app.entitlements.admission import (
    IncompleteAdmissionEvidenceError,
    PaidOperationAdmission,
)
from app.entitlements.cost_models import (
    CapacityReservationRecord,
    SpendReservationRecord,
)
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401
from app.entitlements.reservation_book import capacity_reservation_key
from app.entitlements.types import (
    AdmissionRequest,
    BillableCallSpec,
    CapacityRequest,
    EntitlementRef,
    EntitlementRequirements,
    ExecutionMode,
    FrozenQuote,
    JobSpec,
    OperationPayload,
    ProviderSelection,
    QuoteBundle,
    ReservationBundle,
)
from app.platform.tenancy import TenantContext


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
WORKSPACE_ID = "ws_admission_publish"
CALL_KEY = "call-admission-publish"


class _Policy:
    def authorize(self, session, context, requirements, now):
        return EntitlementRef(
            entitlement_version=7,
            lease_sequence=3,
            period_start=None,
            period_end=None,
            semantic_hash="entitlement-hash",
        )


class _QuoteEngine:
    def bundle(self, calls, *, execution_mode, budget_currency):
        mode = ExecutionMode(execution_mode)
        return QuoteBundle(
            call_quotes=tuple(
                FrozenQuote(
                    call_semantic_key_hash=call.call_semantic_key_hash,
                    provider=call.provider,
                    model=call.model,
                    execution_mode=mode,
                    quantity=call.quantity,
                    provider_amount=Decimal("0") if mode == ExecutionMode.MOCK else Decimal("1"),
                    provider_currency="USD",
                    budget_amount=Decimal("0") if mode == ExecutionMode.MOCK else Decimal("1"),
                    budget_currency=budget_currency,
                    exchange_rate=Decimal("1"),
                    exchange_rate_source="test",
                    exchange_rate_observed_at=NOW,
                    fx_buffer_bps=0,
                    price_rule_version="test",
                    pricing_input_hash="pricing-hash",
                    simulated_amount=None,
                    simulated_currency=None,
                )
                for call in calls
            ),
            total_budget_amount=(
                Decimal("0") if mode == ExecutionMode.MOCK else Decimal(len(calls))
            ),
        )


class _ReservationBook:
    def __init__(
        self,
        *,
        fail: bool = False,
        persist_spend: bool = False,
        persist_capacity: bool = False,
    ) -> None:
        self.fail = fail
        self.persist_spend = persist_spend
        self.persist_capacity = persist_capacity
        self.observed_job_states: tuple[str, ...] = ()

    def reserve(self, session, context, plan):
        jobs = session.scalars(
            select(JobRecord).where(JobRecord.operation_id == plan.operation_id)
        ).all()
        self.observed_job_states = tuple(job.state for job in jobs)
        if self.fail:
            raise RuntimeError("reservation rejected")

        spend_by_call_key = {}
        if self.persist_spend:
            for quote in plan.quote_bundle.call_quotes:
                job_id = plan.job_ids_by_call_key[quote.call_semantic_key_hash]
                reservation_id = f"sr-{job_id}"
                session.add(SpendReservationRecord(
                    id=reservation_id,
                    workspace_id=context.workspace_id,
                    operation_id=plan.operation_id,
                    job_id=job_id,
                    semantic_key_hash=quote.call_semantic_key_hash,
                    state="reserved",
                    provider=quote.provider,
                    execution_mode=str(quote.execution_mode),
                    provider_currency=quote.provider_currency,
                    maximum_provider_amount=quote.provider_amount,
                    budget_currency=quote.budget_currency,
                    maximum_budget_amount=quote.budget_amount,
                    outstanding_budget_amount=quote.budget_amount,
                    entitlement_version=plan.entitlement_ref.entitlement_version,
                    lease_sequence=plan.entitlement_ref.lease_sequence,
                    guardrail_revision=1,
                    platform_policy_version=1,
                    created_at=NOW,
                    updated_at=NOW,
                ))
                spend_by_call_key[quote.call_semantic_key_hash] = reservation_id
            session.flush()

        capacity_ids = []
        if self.persist_capacity:
            first_job_id = next(iter(plan.job_ids_by_call_key.values()))
            for capacity in plan.capacity_requests:
                if capacity.acquisition_phase != "reserve_on_admission":
                    continue
                reservation_id = f"cr-{plan.operation_id}-{capacity.entitlement_key}"
                session.add(CapacityReservationRecord(
                    id=reservation_id,
                    workspace_id=context.workspace_id,
                    operation_id=plan.operation_id,
                    job_id=first_job_id,
                    entitlement_key=capacity.entitlement_key,
                    quantity=int(capacity.quantity),
                    unit=capacity.unit,
                    acquisition_phase="admission",
                    semantic_key_hash=capacity_reservation_key(
                        capacity.entitlement_key, plan.operation_id
                    ),
                    state="reserved",
                    entitlement_version=plan.entitlement_ref.entitlement_version,
                    lease_sequence=plan.entitlement_ref.lease_sequence,
                    created_at=NOW,
                    updated_at=NOW,
                ))
                capacity_ids.append(reservation_id)
            session.flush()

        return ReservationBundle(
            operation_id=plan.operation_id,
            spend_reservation_by_call_key=spend_by_call_key,
            capacity_reservation_ids=tuple(capacity_ids),
            job_ids=tuple(plan.job_ids_by_call_key.values()),
            guardrail_revision=1,
        )


@pytest.fixture()
def engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(WorkspaceRecord(
            id=WORKSPACE_ID,
            name="Admission publish test",
            budget_currency="USD",
            created_at=NOW,
        ))
        session.commit()
    yield engine
    engine.dispose()


def _context() -> TenantContext:
    return TenantContext(
        workspace_id=WORKSPACE_ID,
        role="owner",
        initiator_id="owner-admission-publish",
        initiator_role="owner",
    )


def _request(mode: ExecutionMode = ExecutionMode.MOCK) -> AdmissionRequest:
    return AdmissionRequest(
        command_type="localization.translate_copy",
        semantic_key_hash="semantic-admission-publish",
        request_fingerprint_hash="fingerprint-admission-publish",
        entitlement_requirements=EntitlementRequirements(
            feature_keys=(), numeric_requirements=(),
        ),
        provider_selection=ProviderSelection(
            execution_mode=mode,
            provider="mock" if mode == ExecutionMode.MOCK else "provider",
            model="model",
            binding_scope="workspace",
            credential_binding_id=None,
        ),
        billable_calls=(BillableCallSpec(
            call_semantic_key_hash=CALL_KEY,
            provider="mock" if mode == ExecutionMode.MOCK else "provider",
            model="model",
            quantity=1,
        ),),
        capacity_requests=(),
        operation_payload=OperationPayload(
            command_type="localization.translate_copy",
            result_resource_type="localization_copy_version",
            result_resource_id="copy-version-1",
            payload_version=1,
        ),
        job_specs=(JobSpec(
            job_type="localization.translate_copy",
            resource_type="localization_batch",
            resource_id="batch-1",
            payload_json={"batch_id": "batch-1"},
            call_semantic_key_hash=CALL_KEY,
        ),),
    )


def _admission(reservation_book: _ReservationBook) -> PaidOperationAdmission:
    return PaidOperationAdmission(
        policy=_Policy(),
        quote_engine=_QuoteEngine(),
        reservation_book=reservation_book,
    )


def test_new_jobs_publish_only_after_reservation(engine) -> None:
    reservations = _ReservationBook()
    with Session(engine) as session, session.begin():
        result = _admission(reservations).admit_in_transaction(
            session, _context(), _request(),
        )
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert reservations.observed_job_states == ("pending",)
        assert job is not None
        assert job.state == "queued"
        assert job.execution_contract_version == 1


def test_failed_reservation_rolls_back_non_dispatchable_admission(engine) -> None:
    reservations = _ReservationBook(fail=True)
    with pytest.raises(RuntimeError, match="reservation rejected"):
        with Session(engine) as session, session.begin():
            _admission(reservations).admit_in_transaction(
                session, _context(), _request(),
            )

    with Session(engine) as session:
        assert session.scalar(select(OperationRecord)) is None
        assert session.scalar(select(JobRecord)) is None


def test_idempotent_replay_recovers_legacy_mock_pending_job(engine) -> None:
    request = _request()
    admission = _admission(_ReservationBook())
    with Session(engine) as session, session.begin():
        result = admission.admit_in_transaction(session, _context(), request)

    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert job is not None
        job.state = "pending"
        job.execution_contract_version = 0

    with Session(engine) as session, session.begin():
        replay = admission.admit_in_transaction(session, _context(), request)
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert replay.replayed is True
        assert job is not None
        assert job.state == "queued"
        assert job.execution_contract_version == 1


def test_paid_legacy_recovery_requires_matching_spend_reservation(engine) -> None:
    request = _request(ExecutionMode.BYOK)
    admission = _admission(_ReservationBook())
    with Session(engine) as session, session.begin():
        result = admission.admit_in_transaction(session, _context(), request)

    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert job is not None
        job.state = "pending"
        job.execution_contract_version = 0

    with Session(engine) as session, session.begin():
        with pytest.raises(IncompleteAdmissionEvidenceError, match="spend reservation"):
            admission.admit_in_transaction(session, _context(), request)

    with Session(engine) as session:
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert job is not None
        assert job.state == "pending"


def test_paid_legacy_recovery_accepts_matching_spend_reservation(engine) -> None:
    request = _request(ExecutionMode.BYOK)
    admission = _admission(_ReservationBook(persist_spend=True))
    with Session(engine) as session, session.begin():
        result = admission.admit_in_transaction(session, _context(), request)

    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert job is not None
        job.state = "pending"
        job.execution_contract_version = 0

    with Session(engine) as session, session.begin():
        replay = admission.admit_in_transaction(session, _context(), request)
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert replay.replayed is True
        assert job is not None
        assert job.state == "queued"
        assert job.execution_contract_version == 1


def test_paid_legacy_recovery_rejects_released_spend_reservation(engine) -> None:
    request = _request(ExecutionMode.BYOK)
    admission = _admission(_ReservationBook(persist_spend=True))
    with Session(engine) as session, session.begin():
        result = admission.admit_in_transaction(session, _context(), request)

    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        reservation = session.scalar(select(SpendReservationRecord))
        assert job is not None
        assert reservation is not None
        job.state = "pending"
        job.execution_contract_version = 0
        reservation.state = "released"
        reservation.outstanding_budget_amount = Decimal("0")
        reservation.released_at = NOW

    with Session(engine) as session, session.begin():
        with pytest.raises(IncompleteAdmissionEvidenceError, match="spend reservation"):
            admission.admit_in_transaction(session, _context(), request)

    with Session(engine) as session:
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert job is not None
        assert job.state == "pending"


def test_legacy_recovery_rejects_operation_without_admission_contract(engine) -> None:
    request = _request()
    admission = _admission(_ReservationBook())
    with Session(engine) as session, session.begin():
        result = admission.admit_in_transaction(session, _context(), request)

    with Session(engine) as session, session.begin():
        operation = session.get(OperationRecord, result.operation_id)
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        assert operation is not None
        assert job is not None
        operation.admission_contract_version = 0
        job.state = "pending"
        job.execution_contract_version = 0

    with Session(engine) as session, session.begin():
        with pytest.raises(IncompleteAdmissionEvidenceError, match="admission contract"):
            admission.admit_in_transaction(session, _context(), request)


@pytest.mark.parametrize(
    "reservation_state, publishes",
    [
        ("reserved", True),
        ("released", False),
    ],
)
def test_legacy_recovery_requires_live_capacity_reservation(
    engine, reservation_state: str, publishes: bool,
) -> None:
    request = replace(
        _request(),
        capacity_requests=(CapacityRequest(
            entitlement_key="concurrent_localization_jobs",
            quantity=Decimal("1"),
            unit="job",
            acquisition_phase="reserve_on_admission",
        ),),
    )
    admission = _admission(_ReservationBook(persist_capacity=True))
    with Session(engine) as session, session.begin():
        result = admission.admit_in_transaction(session, _context(), request)

    with Session(engine) as session, session.begin():
        job = session.scalar(
            select(JobRecord).where(JobRecord.operation_id == result.operation_id)
        )
        reservation = session.scalar(select(CapacityReservationRecord))
        assert job is not None
        assert reservation is not None
        job.state = "pending"
        job.execution_contract_version = 0
        reservation.state = reservation_state

    if publishes:
        with Session(engine) as session, session.begin():
            replay = admission.admit_in_transaction(session, _context(), request)
            job = session.scalar(
                select(JobRecord).where(
                    JobRecord.operation_id == result.operation_id
                )
            )
            assert replay.replayed is True
            assert job is not None
            assert job.state == "queued"
    else:
        with Session(engine) as session, session.begin():
            with pytest.raises(
                IncompleteAdmissionEvidenceError,
                match="capacity reservation",
            ):
                admission.admit_in_transaction(session, _context(), request)
        with Session(engine) as session:
            job = session.scalar(
                select(JobRecord).where(
                    JobRecord.operation_id == result.operation_id
                )
            )
            assert job is not None
            assert job.state == "pending"
