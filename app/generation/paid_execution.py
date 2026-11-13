"""Generation dispatch fence: prepare/commit, call, then persist/settle.

Used for the provider-selection UI's frozen prompt recipes, including its
controlled Mock. An ambiguous call is never automatically sent again.
"""
from __future__ import annotations
from app.marketing.blob_read import write_generated

import io
import asyncio
from contextlib import nullcontext
from decimal import Decimal

from PIL import Image
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import GenerationRunRecord, JobRecord, LibraryVersionRecord, WorkspaceRecord, new_id, utcnow
from app.entitlements.cost_models import ProviderCallAttemptRecord, ProviderCostLedgerEntryRecord, SpendReservationRecord
from app.entitlements.credential_protector import CredentialProtector, CredentialEncryptionContext, EncryptedEnvelope
from app.entitlements.quote_engine import CostQuoteEngine
from app.entitlements.reservation_book import ReservationBook
from app.entitlements.types import BillableCallSpec, ReservationPlan, GenerationReservationContext
from app.entitlements.usage import record_provider_call
from app.generation.models import GenerationRecipeSnapshotRecord, ProviderAttemptEventRecord, RunItemAssetVersionBindingRecord
from app.generation.provider_attempt import prepare_attempt, append_event
from app.generation.selection import authorize_paid, credential_binding, SelectionError
from app.generation.worker import _get_run_item, _transition_item, _persist_output, _sync_run_state_best_effort
from app.platform.operations import get_operation, transition_operation, OperationState
from app.providers.adapter import ProviderCallRequest, ProviderDispatchError
from app.providers.router import get_provider
from app.platform.runtime_role import WORKER_ROLE, runs_as


def reserve_run(session, ctx, run, items, job_ids, quote, confirmation_id):
    ref = authorize_paid(session, ctx)
    engine = CostQuoteEngine({run.provider: {run.model: Decimal(quote["unit_maximum"])}}, quote["pricing_version"])
    bundle = engine.bundle(
        [BillableCallSpec(i.id, run.provider, run.model, 1) for i in items],
        execution_mode="byok", budget_currency="USD",
    )
    ReservationBook().reserve(session, ctx, ReservationPlan(
        bundle, (), ref, run.creation_operation_id, job_ids,
        generation_contexts={item.id: GenerationReservationContext(
            item.id, confirmation_id, run.recipe_snapshot_id) for item in items},
    ))


def _settle(session, reservation, attempt_id, *, called: bool):
    if reservation.state in ("settled", "released"):
        return
    session.add(ProviderCostLedgerEntryRecord(
        id=new_id("le"), workspace_id=reservation.workspace_id,
        operation_id=reservation.operation_id, job_id=reservation.job_id,
        spend_reservation_id=reservation.id, run_item_id=reservation.run_item_id,
        provider_call_attempt_id=attempt_id,
        entry_type="estimated_settlement" if called else "release",
        direction="increase" if called else "decrease", execution_mode="byok",
        semantic_key_hash=f"generation:{reservation.id}:{'settle' if called else 'release'}",
        provider=reservation.provider, provider_amount=reservation.maximum_provider_amount,
        provider_currency="USD", budget_amount=reservation.maximum_budget_amount,
        budget_currency="USD", exchange_rate=Decimal("1"), exchange_rate_source="identity",
        created_at=utcnow(),
    ))
    reservation.state = "settled" if called else "released"
    reservation.outstanding_budget_amount = Decimal("0")
    reservation.updated_at = utcnow()
    if called:
        reservation.settled_at = utcnow()
    else:
        reservation.released_at = utcnow()


def _sync_operation(session, ctx, run_id):
    _sync_run_state_best_effort(session, ctx, run_id)
    session.expire_all()
    run = session.get(GenerationRunRecord, run_id)
    operation = get_operation(session, run.creation_operation_id)
    if operation.state in ("succeeded", "failed", "cancelled", "partially_succeeded"):
        return
    if run.status == "uncertain" and operation.state == "running":
        transition_operation(session, operation.id, OperationState.UNCERTAIN,
                             reason="provider outcome requires reconciliation", actor="worker")
    elif run.status in ("succeeded", "failed", "cancelled", "partially_succeeded"):
        transition_operation(session, operation.id, OperationState(run.status),
                             reason="generation items completed", actor="worker")


# Recorded on dispatch_started; an attempt without it was sent with the facts first (before #289).
PROMPT_LAYOUT = "product-facts-last-v1"


def dispatch_prompt(frozen, variant_index):
    """The exact text the image model receives for one variation of a frozen recipe.

    Product facts are claim evidence, not ad copy (#289): sent as frozen, never translated, in a closing
    section after every instruction. A plan's confirmed ad language is the only copy exception the header names."""
    import json
    parts = [frozen["prompt"]]
    if frozen.get("source_image"):
        parts.append("Reference image 1 is the original creative to edit. Any additional references are product photos for appearance fidelity. Apply the requested changes to the original creative.")
    if frozen.get("image_tool"):
        from .image_tools import instruction
        parts.append(instruction(frozen["image_tool"]))
    parts.append(f"Creative variation {variant_index + 1} of {frozen['format']['variants']}.")
    language = (frozen.get("marketing_intent") or {}).get("ad_language")
    header = ("Product facts (claim evidence only, not ad copy): never render their words, labels or language as image text, "
              f"except the quoted ad copy in {language} above." if language else
              "Product facts (claim evidence only, not ad copy): never render their words or labels as image text.")
    return ("\n".join(part for part in parts if part) + "\n\n" + header + "\n"
            + json.dumps(frozen["product"]["content"], ensure_ascii=False))


def _prepare(session, ctx, job_id, item_id):
    item = _get_run_item(session, ctx, item_id)
    run = session.get(GenerationRunRecord, item.generation_run_id)
    snapshot = session.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id)
    if item.state == "running":
        # An earlier process crossed the committed fence and disappeared.
        # A new lease must never cause a second paid dispatch.
        _transition_item(session, ctx, item, state="uncertain", error_summary="provider_outcome_unknown")
        reservation = session.scalar(select(SpendReservationRecord).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.run_item_id == item.id,
        ))
        if reservation and item.execution_mode == "byok":
            reservation.state = "uncertain"
        _sync_operation(session, ctx, run.id)
        return None
    if item.state not in ("pending", "queued"):
        return None
    recipe = snapshot.canonical_json
    frozen = recipe["selection"]
    reservation = session.scalar(select(SpendReservationRecord).where(
        SpendReservationRecord.workspace_id == ctx.workspace_id,
        SpendReservationRecord.run_item_id == item.id,
    ))
    credential_scope = nullcontext(None)
    credential_id = None
    try:
        workspace = session.get(WorkspaceRecord, ctx.workspace_id)
        if not workspace or workspace.lifecycle != "active" or workspace.authorization_epoch != ctx.authorization_epoch:
            raise SelectionError("authorization_changed", "工作区授权已变化。")
        if item.source_asset_version_id and not frozen.get("source_image"):
            raise SelectionError("edit_source_unavailable", "旧修改任务没有冻结原图，请重新准备修改。")
        if frozen.get("source_image"):
            from .image_inputs import check_source
            check_source(session, ctx, frozen["source_image"], frozen["product"]["id"], item.execution_mode)
        if run.status == "cancelled":
            _transition_item(session, ctx, item, state="cancelled")
            if reservation and item.execution_mode == "byok":
                _settle(session, reservation, None, called=False)
            return None
        # Budget was reserved at submission. Check dependency/credential
        # validity without double-counting this Run's reserved exposure.
        from app.marketing.product_references import assert_current
        assert_current(session, ctx, frozen.get("product_references", []))
        if frozen.get("brand_logo"):
            from .selection import _assert_brand_logo
            _assert_brand_logo(session, ctx, frozen["brand_logo"])
        from .selection import product_input
        product_input(session, ctx, frozen["product"]["id"])
        intent = frozen.get("marketing_intent")
        if intent:
            from app.marketing.service import get_project
            project = get_project(session, ctx, intent["project_id"])
            if project.state != "plan_confirmed" or project.revision != intent["plan_revision"]:
                raise SelectionError("marketing_plan_changed", "测试方案已变化，请重新准备修改。")
        feedback = frozen.get("marketing_feedback")
        if feedback:
            from app.marketing.workflow import get_round, scoped, Handoff
            row = get_round(session, ctx, feedback["round_id"])
            if row.state != "reviewing" or scoped(session, ctx, Handoff, round_id=row.id):
                raise SelectionError("feedback_round_frozen", "本轮已冻结，不能继续修改素材。")
        if frozen.get("image_tool"):
            from .image_tools import validate_tool
            validate_tool(frozen["image_tool"], recipe["provider"], recipe["model"])
        version_id = frozen["product"].get("version_id")
        if version_id:
            version = session.scalar(select(LibraryVersionRecord).where(
                LibraryVersionRecord.workspace_id == ctx.workspace_id, LibraryVersionRecord.id == version_id,
            ))
            if not version or version.state == "withdrawn" or version.eligibility == "blocked":
                raise SelectionError("product_unavailable", "冻结的产品版本已不可用。")
        if item.execution_mode == "byok":
            authorize_paid(session, ctx)
            binding = credential_binding(session, ctx, recipe["provider"], include_secret=True)
            if not binding:
                raise SelectionError("credential_required", "供应商凭据已不可用。")
            if not reservation or reservation.state != "reserved":
                raise SelectionError("reservation_required", "生成成本预留已不可用。")
            cred = binding[1]
            credential_id = cred.id
            envelope = EncryptedEnvelope(cred.encrypted_secret, cred.encrypted_data_key, cred.nonce,
                                         cred.encryption_key_id, cred.encryption_algorithm, cred.aad_schema_version)
            enc_ctx = CredentialEncryptionContext(ctx.workspace_id, cred.id, cred.credential_key,
                                                  cred.provider, cred.version_no, cred.aad_schema_version)
            credential_scope = CredentialProtector.local_only().open(envelope, enc_ctx)
        else:
            # Existing ProviderAttempt schema requires a zero reservation
            # identity even for controlled Mock (it has no payable ledger).
            from .worker import _get_or_create_reservation_id
            reservation_id = _get_or_create_reservation_id(session, ctx, item, run.creation_operation_id, job_id)
            reservation = session.get(SpendReservationRecord, reservation_id)
    except Exception as exc:
        code = exc.code if isinstance(exc, SelectionError) else "credential_unavailable"
        _transition_item(session, ctx, item, state="failed", error_summary=code)
        if reservation and item.execution_mode == "byok":
            _settle(session, reservation, None, called=False)
        _sync_operation(session, ctx, run.id)
        return None
    attempt = prepare_attempt(
        session, workspace_id=ctx.workspace_id, operation_id=run.creation_operation_id,
        job_id=job_id, spend_reservation_id=reservation.id, run_item_id=item.id,
        step_kind="generation", call_semantic_key_hash=item.id, attempt_no=0,
        execution_mode=item.execution_mode, provider=recipe["provider"], model=recipe["model"],
        provider_idempotency_key=item.id, credential_version_id=credential_id,
        request_fingerprint_hash=item.semantic_fingerprint,
        chargeability="charged" if item.execution_mode == "byok" else "legacy_reserved",
    )
    _transition_item(session, ctx, item, state="running")
    append_event(session, workspace_id=ctx.workspace_id, provider_call_attempt_id=attempt.id,
                 event_type="dispatch_started", payload={"provider": recipe["provider"], "model": recipe["model"],
                                                         "prompt_layout": PROMPT_LAYOUT},
                 executor="worker")
    prompt = dispatch_prompt(frozen, item.variant_index)
    return {"attempt_id": attempt.id, "run_id": run.id, "recipe": recipe,
            "credential_scope": credential_scope, "prompt": prompt, "reservation_id": reservation.id}


@runs_as(WORKER_ROLE)
def record_execution_failure(ctx, job_id, item_id):
    """Close pre-dispatch failures; preserve exposure after a committed fence.

    Also repairs a failed Job whose transaction rolled back before the fence.
    This function never dispatches a provider or retries a Job.
    """
    if not ctx.is_system:
        raise SelectionError("worker_required", "Only the Worker can settle an execution failure.")
    with tenant_session(ctx) as session:
        item = _get_run_item(session, ctx, item_id)
        job = session.get(JobRecord, job_id)
        if not job or job.workspace_id != ctx.workspace_id or job.resource_id != item.id or job.type != "generation.run_item":
            raise SelectionError("job_mismatch", "The Job does not own this generation item.")
        if item.state not in ("pending", "queued", "running"):
            return
        attempts = session.scalars(select(ProviderCallAttemptRecord).where(
            ProviderCallAttemptRecord.workspace_id == ctx.workspace_id,
            ProviderCallAttemptRecord.run_item_id == item.id)).all()
        reservations = session.scalars(select(SpendReservationRecord).where(
            SpendReservationRecord.workspace_id == ctx.workspace_id,
            SpendReservationRecord.run_item_id == item.id,
            SpendReservationRecord.job_id == job.id)).all()
        before_dispatch = item.state in ("pending", "queued") and not attempts
        _transition_item(session, ctx, item,
            state="failed" if before_dispatch else "uncertain",
            error_summary="generation_preparation_failed" if before_dispatch else "provider_outcome_unknown")
        for reservation in reservations:
            if reservation.state == "reserved":
                if before_dispatch:
                    _settle(session, reservation, None, called=False)
                else:
                    reservation.state = "uncertain"
        _sync_operation(session, ctx, item.generation_run_id)


@runs_as(WORKER_ROLE)
async def execute_run_item(ctx, job_id, item_id, blob_store, *, adapter=None):
    # Read and hash-check product photos before committing the paid-call fence.
    references = []
    edit_mask = None
    source_content = None
    tool_spec = None
    try:
        with tenant_session(ctx) as session:
            item = _get_run_item(session, ctx, item_id)
            run = session.get(GenerationRunRecord, item.generation_run_id)
            snap = session.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id)
            frozen = snap.canonical_json.get("selection", {})
            source = frozen.get("source_image")
            tool_spec = frozen.get("image_tool")
            frozen_refs = frozen.get("product_references", [])
            # The brand logo travels last, after product photos, as the brief states.
            brand_logo = frozen.get("brand_logo")
            if item.state not in ("pending", "queued"):
                frozen_refs = []
                source = None
                brand_logo = None
        from app.marketing.blob_read import read_committed
        from app.platform.blob_store import CommittedBlobRef
        import hashlib
        if source:
            from .image_inputs import read_source
            source_content = await read_source(blob_store, source)
            if tool_spec:
                from .image_tools import prepare_input, validate_tool
                validate_tool(tool_spec, snap.canonical_json["provider"], snap.canonical_json["model"])
                prepared_source, edit_mask = await asyncio.to_thread(prepare_input, source_content, tool_spec)
                references.append(prepared_source)
            else:
                references.append(source_content)
        for ref in frozen_refs:
            content = await read_committed(blob_store, CommittedBlobRef(**ref["blob"]), 20 * 1024 * 1024)
            if hashlib.sha256(content).hexdigest() != ref["content_sha256"]:
                raise ValueError("product_reference_hash_changed")
            references.append(content)
        if brand_logo:
            content = await read_committed(blob_store, CommittedBlobRef(**brand_logo["blob"]), 20 * 1024 * 1024)
            if hashlib.sha256(content).hexdigest() != brand_logo["content_sha256"]:
                raise ValueError("brand_logo_hash_changed")
            references.append(content)
    except Exception:
        record_execution_failure(ctx, job_id, item_id)
        raise
    with tenant_session(ctx) as session:
        prepared = _prepare(session, ctx, job_id, item_id)
    if prepared is None:
        return
    recipe = prepared["recipe"]
    fmt = recipe["selection"]["format"]
    provider = adapter or get_provider(recipe["provider"])
    result = None
    try:
        with prepared["credential_scope"] as credential:
            result = await provider.execute(ProviderCallRequest(
                prompt=prepared["prompt"], reference_inputs=tuple(references), aspect_ratio=fmt["aspect_ratio"],
                image_size=fmt["image_size"], model=recipe["model"], idempotency_key=item_id, credential=credential,
                edit_operation=tool_spec["kind"] if tool_spec else None, edit_mask=edit_mask,
            ))
        if references and recipe["execution_mode"] == "byok":
            from app.providers.base import reference_delivery_metadata
            expected_delivery = reference_delivery_metadata(references)
            if any(result.metadata.get(k) != v for k, v in expected_delivery.items()):
                raise ValueError("provider_reference_delivery_unverified")
        tool_evidence = None
        if tool_spec:
            from .image_tools import delivery_metadata, finish
            if recipe["execution_mode"] == "byok" and any(result.metadata.get(k) != v for k, v in delivery_metadata(tool_spec["kind"], edit_mask).items()):
                raise ValueError("provider_image_tool_delivery_unverified")
            content, tool_evidence = await asyncio.to_thread(finish, source_content, result.output_bytes, tool_spec)
            tool_evidence["delivery"] = delivery_metadata(tool_spec["kind"], edit_mask)
        else:
            content = result.output_bytes
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
            mime = Image.MIME[image.format]
        if mime not in ("image/png", "image/jpeg", "image/webp"):
            raise ValueError("unsupported provider output")
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[mime]
        key = f"committed/{ctx.workspace_id}/generation/{prepared['attempt_id']}.{extension}"
        blob = await write_generated(blob_store, key, content, mime)
    except Exception as exc:
        certain_no_call = isinstance(exc, ProviderDispatchError) and exc.dispatch_certainty == "not_dispatched"
        from .image_tools import ImageToolOutputError
        invalid_tool_output = isinstance(exc, ImageToolOutputError)
        with tenant_session(ctx) as session:
            item = _get_run_item(session, ctx, item_id)
            reservation = session.get(SpendReservationRecord, prepared["reservation_id"])
            # If output arrived but storage failed, charge is known; the
            # output needs reconciliation, never another provider call.
            uncertain = not certain_no_call and not invalid_tool_output and recipe["execution_mode"] == "byok"
            failure_code = "provider_outcome_unknown" if uncertain else "image_tool_output_invalid" if invalid_tool_output else "provider_rejected"
            append_event(session, workspace_id=ctx.workspace_id,
                         provider_call_attempt_id=prepared["attempt_id"], event_type="failure_classified",
                         payload={"classification": "uncertain" if uncertain else "terminal",
                                  "code": failure_code}, executor="worker")
            _transition_item(session, ctx, item, state="uncertain" if uncertain else "failed",
                             error_summary=failure_code)
            if recipe["execution_mode"] == "byok":
                if result is not None:
                    _settle(session, reservation, prepared["attempt_id"], called=True)
                elif certain_no_call:
                    _settle(session, reservation, prepared["attempt_id"], called=False)
                else:
                    reservation.state = "uncertain"
            if result is not None:
                # The provider answered and only its output is unusable (or
                # could not be stored): the call happened. Without a result
                # the call either never left or has an unknown outcome.
                attempt = session.get(ProviderCallAttemptRecord, prepared["attempt_id"])
                record_provider_call(session, ctx, attempt, occurred_at=utcnow())
            _sync_operation(session, ctx, prepared["run_id"])
        return
    # The board/gallery derivative is best effort: a failure is logged and leaves the version on its original.
    from .thumbnails import commit_thumbnail_best_effort
    thumbnail = await commit_thumbnail_best_effort(blob_store, ctx.workspace_id, content,
        log_context={"run_item_id": item_id, "object_key": blob.committed_key})
    with tenant_session(ctx) as session:
        item = _get_run_item(session, ctx, item_id)
        attempt = session.get(ProviderCallAttemptRecord, prepared["attempt_id"])
        import hashlib
        output = {"object_key": blob.committed_key, "storage_version": blob.storage_version_id,
                  "content_sha256": hashlib.sha256(content).hexdigest(), "byte_size": len(content), "mime_type": mime}
        if tool_evidence:
            output["image_tool"] = tool_evidence
            from math import gcd
            divisor = gcd(tool_evidence["width"], tool_evidence["height"])
            output["aspect_ratio"] = f"{tool_evidence['width'] // divisor}:{tool_evidence['height'] // divisor}"
        append_event(session, workspace_id=ctx.workspace_id, provider_call_attempt_id=attempt.id,
                     event_type="response_received", payload={**output, "provider_request_id": result.provider_request_id,
                              "reference_delivery": {k: result.metadata[k] for k in ("reference_delivery_policy", "reference_input_sha256") if k in result.metadata}},
                     executor="worker")
        from app.adapters.persistence.models import AssetRecord, AssetVersionRecord
        source = session.get(AssetVersionRecord, item.source_asset_version_id) if item.source_asset_version_id else None
        if source:
            session.scalar(select(AssetRecord).where(AssetRecord.workspace_id == ctx.workspace_id,
                AssetRecord.id == source.asset_id).with_for_update())
        existing = session.scalar(select(AssetVersionRecord).where(
            AssetVersionRecord.workspace_id == ctx.workspace_id, AssetVersionRecord.asset_id == source.asset_id,
            AssetVersionRecord.content_sha256 == output["content_sha256"])) if source else None
        if existing:
            version = existing
            asset = session.get(AssetRecord, version.asset_id)
            _transition_item(session, ctx, item, state="succeeded")
        else:
            asset, version = _persist_output(session, ctx, item, attempt, output, committed_blob_ref=blob, committed_thumbnail=thumbnail)
        if not existing and recipe["selection"].get("product_references"):
            version.reference_sources_json = {"policy": "marketing-product-photos-v1",
                "reference_actor_id": recipe["selection"].get("reference_actor_id"),
                "sent_to_provider": item.execution_mode == "byok",
                "delivery": {k: result.metadata[k] for k in ("reference_delivery_policy", "reference_input_sha256") if k in result.metadata},
                "references": [{k: ref[k] for k in ("reference_version_id", "content_sha256", "content_hash", "source", "attestation")}
                    for ref in recipe["selection"]["product_references"]]}
        if not item.source_asset_version_id:
            asset.prompt = prepared["prompt"]
        session.add(RunItemAssetVersionBindingRecord(id=new_id("riavb"), workspace_id=ctx.workspace_id,
                    run_item_id=item.id, asset_version_id=version.id, kind="reused" if existing else "produced",
                    producing_provider_attempt_id=attempt.id, created_at=utcnow()))
        session.flush()
        # Validate the actual file before recording deterministic format QC.
        # Human quality review remains pending on this new AssetVersion.
        from .qc import run_qc
        if not existing:
            run_qc(session, ctx, version.id, item.id)
        if recipe["execution_mode"] == "byok":
            reservation = session.get(SpendReservationRecord, prepared["reservation_id"])
            _settle(session, reservation, attempt.id, called=True)
        # Counted for the Controlled Mock too, in its own ``mock`` bucket.
        record_provider_call(session, ctx, attempt, occurred_at=utcnow())
        _sync_operation(session, ctx, prepared["run_id"])


def asset_blob_ref(session, ctx, version_id, *, version=None, storage_versions=None):
    from app.adapters.persistence.models import AssetVersionRecord
    from app.platform.blob_store import CommittedBlobRef
    from .run_queries import GenerationResourceNotFoundError
    if version is None:
        version = session.scalar(select(AssetVersionRecord).where(
            AssetVersionRecord.workspace_id == ctx.workspace_id, AssetVersionRecord.id == version_id,
        ))
    if not version or version.id != version_id or version.workspace_id != ctx.workspace_id:
        raise GenerationResourceNotFoundError("asset_version", version_id, code="asset_version_not_found")
    if version.committed_object_key and version.committed_storage_version_id:
        import base64
        return CommittedBlobRef(version.committed_object_key, version.committed_storage_version_id,
            version.byte_size, base64.b64encode(bytes.fromhex(version.content_sha256)).decode(), version.mime_type)
    if storage_versions is None:
        event = session.scalar(select(ProviderAttemptEventRecord).where(
            ProviderAttemptEventRecord.workspace_id == ctx.workspace_id,
            ProviderAttemptEventRecord.provider_call_attempt_id == version.provider_attempt_id,
            ProviderAttemptEventRecord.event_type == "response_received",
        ))
        storage_version = (event.payload_json if event else {}).get("storage_version")
    else:
        storage_version = storage_versions.get(version.provider_attempt_id)
    if not storage_version:
        raise SelectionError("output_unavailable", "该历史素材暂无可下载文件。")
    return CommittedBlobRef(version.object_key, storage_version, version.byte_size, "", version.mime_type)


def asset_blob_refs(session, ctx, versions):
    """Batch legacy storage receipts while preserving per-version failures."""
    from dataclasses import asdict
    attempts = [v.provider_attempt_id for v in versions if not (v.committed_object_key and v.committed_storage_version_id)]
    storage = {}
    if attempts:
        for event in session.scalars(select(ProviderAttemptEventRecord).where(
            ProviderAttemptEventRecord.workspace_id == ctx.workspace_id,
            ProviderAttemptEventRecord.provider_call_attempt_id.in_(attempts),
            ProviderAttemptEventRecord.event_type == 'response_received')).all():
            storage.setdefault(event.provider_call_attempt_id, event.payload_json.get('storage_version'))
    refs = {}
    for version in versions:
        try:
            refs[version.id] = asdict(asset_blob_ref(session, ctx, version.id, version=version, storage_versions=storage))
        except SelectionError:
            refs[version.id] = None
    return refs
