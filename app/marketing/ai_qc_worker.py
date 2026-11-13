"""Image reads precede the committed paid-call fence. Unknown calls never resend."""
from __future__ import annotations
from .blob_read import read_committed
import hashlib
import json
import httpx
from pydantic import ValidationError
from sqlalchemy import select
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, new_id, utcnow
from app.entitlements.cost_models import SpendReservationRecord, ProviderCallAttemptRecord
from app.entitlements.credential_protector import CredentialProtector, CredentialEncryptionContext, EncryptedEnvelope
from app.entitlements.usage import record_provider_call
from app.generation.paid_execution import _settle
from app.generation.selection import authorize_paid, credential_binding
from app.platform.blob_store import CommittedBlobRef
from app.platform.operations import OperationState, OperationResultRef, get_operation, transition_operation
from app.llm import extract_json
from .ai_qc_models import MarketingAIQCRecord as Call
from .ai_qc_provider import visual_completion, simulated_completion, prepare_image
from .ai_qc import QCOutputError, normalize_report, input_is_current
from .service import ensure_active
from . import workflow
from app.platform.runtime_role import WORKER_ROLE, runs_as


EXCERPT_CHARS = 2000


def rejected_output(raw, error):
    """Why a returned output was refused, kept on the call (#295): the reason, where, and a bounded excerpt plus hash of
    exactly what the provider returned. Only the output is kept, never the request, its headers or the credential."""
    if isinstance(error, ValidationError):
        first = error.errors(include_url=False)[0]
        problem = first["type"]
        error = QCOutputError("qc_output_schema_invalid", ".".join(str(part) for part in first["loc"]) or None)
    else:
        problem = None
    returned = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
    return {"reason": error.code if isinstance(error, QCOutputError) else "qc_output_invalid",
        "field": error.field if isinstance(error, QCOutputError) else None, "problem": problem,
        "excerpt": returned[:EXCERPT_CHARS], "sha256": hashlib.sha256(returned.encode()).hexdigest(), "length": len(returned)}


def get_call(session, ctx, call_id):
    return session.scalar(select(Call).where(Call.workspace_id == ctx.workspace_id, Call.id == call_id).with_for_update())


def reservation_for(session, ctx, call):
    return session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.workspace_id == ctx.workspace_id,
        SpendReservationRecord.job_id == call.job_id))


def finish_operation(session, ctx, call, state):
    op = get_operation(session, call.operation_id)
    if op.state == state.value:
        return
    transition_operation(session, op.id, state, call.error_code or "ai_qc_completed", ctx.executor,
        result_ref=OperationResultRef(resource_type="marketing_ai_qc", resource_id=call.id))


def uncertain(session, ctx, call):
    call.state, call.error_code = "uncertain", "qc_provider_outcome_unknown"
    reservation = reservation_for(session, ctx, call)
    if reservation:
        reservation.state = "uncertain"
    attempt = session.scalar(select(ProviderCallAttemptRecord).where(ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
        ProviderCallAttemptRecord.call_semantic_key_hash == call.id))
    if attempt:
        attempt.dispatch_state, attempt.error_code = "uncertain", call.error_code
    finish_operation(session, ctx, call, OperationState.UNCERTAIN)


@runs_as(WORKER_ROLE)
async def execute_ai_qc(ctx, call_id, blob_store, *, complete=visual_completion):
    credential, attempt_id = None, None
    with tenant_session(ctx) as session:
        call = get_call(session, ctx, call_id)
        if not call or call.state not in ("queued", "running"):
            return
        if call.state == "running":
            uncertain(session, ctx, call)
            return
        frozen = call.input_json
    # Reading storage and image decoding cannot incur an AI fee. A crash here
    # leaves the call queued, so a recovered job may safely repeat preparation.
    try:
        images = []
        for image in frozen["images"]:
            content = await read_committed(blob_store, CommittedBlobRef(**image["blob"]), 20 * 1024 * 1024)
            images.append(prepare_image(content, image["content_sha256"]))
    except Exception:
        with tenant_session(ctx) as session:
            call = get_call(session, ctx, call_id)
            if not call or call.state != "queued":
                return
            transition_operation(session, call.operation_id, OperationState.RUNNING, "qc_preparation_failed", ctx.executor)
            call.state, call.error_code = "failed", "qc_image_unavailable"
            reservation = reservation_for(session, ctx, call)
            if reservation:
                _settle(session, reservation, None, called=False)
            finish_operation(session, ctx, call, OperationState.FAILED)
        return
    with tenant_session(ctx) as session:
        call = get_call(session, ctx, call_id)
        if call.state != "queued":
            return
        transition_operation(session, call.operation_id, OperationState.RUNNING, "ai_qc_started", ctx.executor)
        reservation = reservation_for(session, ctx, call)
        try:
            ensure_active(session, ctx)
            row = workflow.get_round(session, ctx, call.round_id)
            job = session.get(JobRecord, call.job_id)
            if row.state != "reviewing" or not job or job.workspace_id != ctx.workspace_id or job.state != "running":
                raise ValueError("qc_admission_changed")
            if not input_is_current(session, ctx, row, call):
                raise ValueError("qc_input_changed")
            if call.execution_mode == "byok":
                authorize_paid(session, ctx)
                binding = credential_binding(session, ctx, call.provider, include_secret=True)
                if not binding or not reservation or reservation.state != "reserved":
                    raise ValueError("qc_credential_unavailable")
                cred = binding[1]
                envelope = EncryptedEnvelope(cred.encrypted_secret, cred.encrypted_data_key, cred.nonce,
                    cred.encryption_key_id, cred.encryption_algorithm, cred.aad_schema_version)
                encryption_ctx = CredentialEncryptionContext(ctx.workspace_id, cred.id, cred.credential_key,
                    cred.provider, cred.version_no, cred.aad_schema_version)
                with CredentialProtector.local_only().open(envelope, encryption_ctx) as raw:
                    credential = bytes(raw)
                attempt_id = new_id("pca")
                session.add(ProviderCallAttemptRecord(id=attempt_id, workspace_id=ctx.workspace_id,
                    operation_id=call.operation_id, job_id=call.job_id, spend_reservation_id=reservation.id,
                    call_semantic_key_hash=call.id, attempt_no=0, execution_mode="byok", provider=call.provider,
                    credential_version_id=cred.id, dispatch_state="prepared", prepared_at=utcnow(), updated_at=utcnow()))
        except Exception:
            call.state, call.error_code = "failed", "qc_admission_changed"
            if reservation:
                _settle(session, reservation, None, called=False)
            finish_operation(session, ctx, call, OperationState.FAILED)
            return
        call.state = "running"  # committed before the provider can receive bytes
        provider, model, mode = call.provider, call.model, call.execution_mode
    known_error = None
    try:
        data = simulated_completion(frozen, images) if mode == "mock" else await complete(provider, model, credential, frozen, images)
    except (httpx.HTTPStatusError, ValueError):
        # A received refusal, HTTP response or invalid JSON is a known outcome.
        data, known_error = None, "qc_provider_rejected"
    except Exception:
        with tenant_session(ctx) as session:
            call = get_call(session, ctx, call_id)
            if call.state == "running":
                uncertain(session, ctx, call)
        return
    finally:
        credential = None
    with tenant_session(ctx) as session:
        call = get_call(session, ctx, call_id)
        if call.state not in ("running", "uncertain"):
            return
        reservation = reservation_for(session, ctx, call)
        if reservation:
            _settle(session, reservation, attempt_id, called=True)
        if attempt_id:  # only BYOK has an attempt; the Mock report is simulated locally
            attempt = session.get(ProviderCallAttemptRecord, attempt_id)
            attempt.dispatch_state, attempt.completed_at = "succeeded", utcnow()
            # A refusal or invalid output is an answer too: the provider was called.
            record_provider_call(session, ctx, attempt, occurred_at=attempt.completed_at)
        raw = data
        try:
            if known_error:
                raise ValueError(known_error)
            receipt = {"usage": getattr(data, "usage", {}), "request_id": getattr(data, "request_id", "")}
            if isinstance(data, str):
                if len(data.encode()) > 131072:
                    raise QCOutputError("qc_output_too_large")
                try:
                    data = extract_json(data)
                except ValueError as exc:
                    raise QCOutputError("qc_output_not_json") from exc
            normalized = normalize_report(data, frozen)
            if receipt["request_id"] or receipt["usage"]:
                normalized["provider_receipt"] = receipt
            normalized["source"] = "controlled_mock" if mode == "mock" else "ai_visual_suggestion"
            normalized["image_evidence"] = [{"asset_version_id": meta["asset_version_id"],
                **{k: v for k, v in image.items() if k != "bytes"}} for meta, image in zip(frozen["images"], images)]
            call.state, call.result_json, call.error_code = "succeeded", normalized, None
            finish_operation(session, ctx, call, OperationState.SUCCEEDED)
            row = workflow.get_round(session, ctx, call.round_id)
            workflow.audit(session, row, "ai_qc_completed", {"call_id": call.id, "input_hash": call.input_hash, "source": normalized["source"]})
        except (ValidationError, ValueError, TypeError) as exc:
            call.state, call.error_code = "failed", "qc_output_invalid" if not known_error else known_error
            if not known_error:
                call.error_detail = rejected_output(raw, exc)
            finish_operation(session, ctx, call, OperationState.FAILED)
