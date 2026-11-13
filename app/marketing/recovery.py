"""Explicitly abandon a missing result, retaining the full estimated cost.

The API only admits an owner-confirmed reconciliation job. The Worker owns
settlement and call updates; neither path sends another provider request.
"""
from decimal import Decimal
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord
from app.entitlements.cost_models import SpendReservationRecord, ProviderCallAttemptRecord
from app.generation.paid_execution import _settle
from app.platform.audit import append_event
from app.platform.jobs import enqueue_in_session
from app.platform.operations import create_operation, get_operation, transition_operation, OperationState
from .contracts import Document, Text
from .models import PlanningCallRecord
from .ai_qc_models import MarketingAIQCRecord
from .service import MarketingError, ensure_active
from app.platform.runtime_role import WORKER_ROLE, runs_as


class ResolveCall(Document):
    kind: Literal['planning', 'ai_qc']
    maximum_cost: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=14, decimal_places=6)
    acknowledge_estimated_cost: Literal[True]
    note: Text


class ResolutionSummary(Document):
    id: str
    operation_id: str
    call_id: str
    state: str


def get_call(session, ctx, kind, call_id, *, lock=False):
    cls = PlanningCallRecord if kind == 'planning' else MarketingAIQCRecord
    query = select(cls).where(cls.workspace_id == ctx.workspace_id, cls.id == call_id)
    call = session.scalar(query.with_for_update() if lock else query)
    if call is None:
        raise MarketingError('call_not_found', '找不到需要核对的任务。', 404)
    return call


def admit(session, ctx, call_id, body):
    ensure_active(session, ctx)
    if ctx.role != 'owner':
        raise MarketingError('owner_required', '需要工作区 Owner 核对费用并结束任务。', 403)
    call = get_call(session, ctx, body.kind, call_id)
    if call.state != 'uncertain' and call.error_code != 'closed_without_result':
        raise MarketingError('call_not_uncertain', '仅可核对结果不确定的任务。', 409)
    if Decimal(call.maximum_cost) != body.maximum_cost:
        raise MarketingError('recovery_cost_changed', '确认的费用上限与任务不一致，请刷新后核对。', 409)
    op = create_operation(session, command_type='marketing.resolve_call')
    enqueue_in_session(session, context=ctx, job_type='marketing.resolve_call',
        payload={'operation_id': op.id, 'call_id': call_id, **body.model_dump(mode='json')},
        idempotency_key=op.id, resource_type='marketing_call_resolution', resource_id=call_id,
        safe_to_retry_after_crash=True)
    return op


@runs_as(WORKER_ROLE)
def execute_resolution(ctx, operation_id):
    with tenant_session(ctx) as session:
        op = get_operation(session, operation_id)
        if op.state in ('succeeded', 'failed', 'cancelled'):
            return
        if op.state == 'queued':
            transition_operation(session, op.id, OperationState.RUNNING, 'reconciliation_started', ctx.executor)
        job = session.scalar(select(JobRecord).where(JobRecord.workspace_id == ctx.workspace_id,
            JobRecord.type == 'marketing.resolve_call', JobRecord.idempotency_key == op.id))
        if not job or job.initiator_role != 'owner':
            raise MarketingError('owner_required', '恢复任务缺少 Owner 确认。', 403)
        body = ResolveCall.model_validate({k: v for k, v in job.payload_json.items() if k not in ('operation_id', 'call_id')})
        call = get_call(session, ctx, body.kind, job.payload_json['call_id'], lock=True)
        if call.error_code == 'closed_without_result':
            transition_operation(session, op.id, OperationState.SUCCEEDED, 'already_reconciled', ctx.executor)
            return
        if call.state != 'uncertain' or Decimal(call.maximum_cost) != body.maximum_cost:
            transition_operation(session, op.id, OperationState.FAILED, 'call_state_changed', ctx.executor)
            return
        ensure_active(session, ctx)
        reservation = session.scalar(select(SpendReservationRecord).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.job_id == call.job_id).with_for_update())
        attempt = session.scalar(select(ProviderCallAttemptRecord).where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.job_id == call.job_id))
        if call.execution_mode == 'byok':
            if reservation is None:
                raise MarketingError('reservation_missing', '缺少费用预留，需管理员核对。', 409)
            _settle(session, reservation, attempt.id if attempt else None, called=True)
        # Provider dispatch certainty remains unknown. Only the local result
        # is abandoned, with conservative estimated settlement and audit proof.
        previous_error = call.error_code
        call.state, call.error_code = 'failed', 'closed_without_result'
        transition_operation(session, call.operation_id, OperationState.FAILED, 'owner_closed_without_result', ctx.executor)
        append_event(session, event_type='marketing.call_reconciled', resource_type=body.kind,
            resource_id=call.id, details={'resolution': 'closed_without_result', 'previous_error': previous_error,
                'maximum_estimated_cost': str(body.maximum_cost), 'provider_outcome': 'unknown',
                'note': body.note, 'resolution_operation_id': op.id})
        transition_operation(session, op.id, OperationState.SUCCEEDED, 'closed_without_result', ctx.executor)


@runs_as(WORKER_ROLE)
def fail_resolution(ctx, operation_id):
    """Make an exhausted recovery attempt visible without changing the call."""
    with tenant_session(ctx) as session:
        op = get_operation(session, operation_id)
        if op.state == 'queued':
            transition_operation(session, op.id, OperationState.RUNNING, 'reconciliation_started', ctx.executor)
        if op.state in ('queued', 'running'):
            transition_operation(session, op.id, OperationState.FAILED, 'reconciliation_failed', ctx.executor)
