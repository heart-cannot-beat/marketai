"""Persistent worker runtime for generation jobs."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import os
import signal
import socket
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from . import config, store
from .platform import jobs, upload_intents
from .platform.job_outcomes import (
    HandlerOutcomeSignal,
    JobFailed,
    JobRetry,
    settle_job_outcome,
)
from .providers.base import ProviderError
from .providers.router import get_provider
from .adapters.meta.client import MetaApiError, MetaClient
from .adapters.meta.write_client import HttpMetaWriteAdapter, MetaWriteError, MetaWritePort
from .meta import service as meta_service
from .launch import service as launch_service
from .launch.recovery import RecoveryError, execute_recovery
from .launch.saga import (
    ProvisioningError,
    ProvisioningUncertain,
    execute_provisioning,
    record_provisioning_failure,
)
from .adapters.persistence.database import SessionLocal, assert_runtime_role, tenant_session
from .platform.blob_store_factory import build_blob_store
from .platform.blob_store import BlobRef, BlobStoreError
from .platform.runtime_role import WORKER_ROLE, runs_as
from .platform.operations import (
    TERMINAL_STATES,
    OperationRecord,
    OperationResultRef,
    OperationState,
    get_operation,
    transition_operation,
)
from .platform.tenancy import TenantContext
from .platform.workspace_admission import WorkspaceAdmissionReader
from .providers.dispatch import ExternalCallCoordinator as ProviderCallCoordinator


logger = logging.getLogger(__name__)

# A database error fails one tick, not the Worker: a release converging the
# runtime grants, a failover or a dropped connection all clear on their own,
# and a claimed Job returns to the queue when its lease expires either way.
# Errors that persist past this many consecutive ticks still end the process,
# so a Worker that cannot reach its tables fails loudly and is restarted.
_MAX_CONSECUTIVE_DB_ERRORS = 5
_DB_ERROR_BACKOFF_CAP_SECONDS = 30.0

_LOCALIZATION_JOB_TYPES = frozenset(
    {
        "localization.analyze_source",
        "localization.translate_copy",
        "localization.edit_attempt",
        "localization.run_deterministic_qc",
        "localization.ocr_check",
        "localization.visual_qc",
        "localization.finalize_item",
        "localization.fold_batch",
    }
)


def _locked_operation(session, context: TenantContext, operation_id: str) -> OperationRecord:
    """Read the Operation under ``SELECT ... FOR UPDATE``, as committed now.

    SQLite ignores the clause and serializes writers itself.
    """
    return session.scalars(
        select(OperationRecord)
        .where(
            OperationRecord.id == operation_id,
            OperationRecord.workspace_id == context.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _settle_running_operation(
    context: TenantContext,
    operation_id: str,
    to_state: OperationState,
    *,
    reason: str | None,
    result_ref: OperationResultRef | None = None,
) -> str | None:
    """Move a ``running`` Operation to ``to_state`` under its row lock.

    Returns ``None`` when this call moved it, or the state it found when
    another run of the same Job got there first -- which then writes nothing.
    """
    with tenant_session(context) as session:
        state = _locked_operation(session, context, operation_id).state
        if state != OperationState.RUNNING.value:
            return state
        transition_operation(
            session, operation_id, to_state,
            reason=reason, actor=context.executor, result_ref=result_ref,
        )
    return None


class WorkerRuntime:
    def __init__(self, owner: str | None = None, meta_client: MetaClient | None = None,
                 launch_adapter: MetaWritePort | None = None,
                 coordinator=None, localization_coordinator=None) -> None:
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.meta_client = meta_client
        self.launch_adapter = launch_adapter
        self.stopping = False
        self._drain_started_at: float | None = None
        # spec §7.8: the Worker checks domain-side projections only, via the
        # same PostgreSQL-only `marketai_workspace_admission` function the
        # delegated identity middleware uses -- see the note on
        # `app.platform.upload_intents.verify_upload_intent`.
        self.blob_store = build_blob_store()
        self.admission_reader = (
            WorkspaceAdmissionReader(SessionLocal) if config.DELEGATED_IDENTITY_ENABLED else None
        )
        # ExternalCallCoordinator for new-contract jobs (execution_contract_version >= 1).
        # Injected for testability; None means the coordinated path is unavailable and
        # any new-contract job will fail with a clear error.
        self._coordinator = coordinator
        # Localization's coordinator has a different, domain-agnostic
        # evidence-recorder contract from the legacy Generation coordinator.
        # Build the real composition-root dependency here so the production
        # Worker consumes the same durable Jobs the API admits.
        self._localization_coordinator = (
            localization_coordinator
            if localization_coordinator is not None
            else ProviderCallCoordinator(tenant_session)
        )
        self._onboarding_http_client = (
            httpx.AsyncClient()
            if config.CONTROL_PLANE_INTERNAL_BASE_URL
            and config.DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN
            else None
        )
        # Monotonic time of the next Meta observation dispatch (#107); None
        # means the first main-lane tick dispatches.
        self._next_observation_dispatch_at: float | None = None

    def _begin_drain(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        self._drain_started_at = time.monotonic()
        from .platform.ops_alerts import AlertCategory, Severity, emit

        emit(
            Severity.INFO,
            AlertCategory.SHUTDOWN,
            "SIGTERM received, draining in-flight work",
            details={"owner": self.owner},
        )

    async def run_forever(self) -> None:
        generation = ('generate_asset', 'generation.run_item')
        qc = ('marketing.ai_qc',)
        planning = ('marketing.plan',)
        lanes = (
            (config.WORKER_GENERATION_CONCURRENCY, generation, ()),
            (config.WORKER_QC_CONCURRENCY, qc, ()),
            (config.WORKER_PLANNING_CONCURRENCY, planning, ()),
            (config.WORKER_GENERAL_CONCURRENCY, None, generation + qc + planning),
        )
        tasks = [asyncio.create_task(self._run_lane(types, excluded))
            for concurrency, types, excluded in lanes for _ in range(concurrency)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        drain_seconds = (
            time.monotonic() - self._drain_started_at
            if self._drain_started_at is not None
            else 0.0
        )
        from .platform.ops_alerts import AlertCategory, Severity, emit

        emit(
            Severity.INFO,
            AlertCategory.SHUTDOWN,
            "Worker drain completed",
            details={"owner": self.owner, "drain_seconds": round(drain_seconds, 2)},
        )

    async def _run_lane(self, job_types, exclude_types) -> None:
        consecutive_db_errors = 0
        while not self.stopping:
            try:
                handled = await self.run_once(job_types=job_types, exclude_types=exclude_types)
            except DBAPIError as exc:
                consecutive_db_errors += 1
                if consecutive_db_errors >= _MAX_CONSECUTIVE_DB_ERRORS:
                    raise
                await self._back_off_after_db_error(exc, consecutive_db_errors)
                continue
            consecutive_db_errors = 0
            if not handled:
                await asyncio.sleep(config.JOB_POLL_SECONDS)

    async def _back_off_after_db_error(self, exc: DBAPIError, consecutive: int) -> None:
        delay = min(
            config.JOB_POLL_SECONDS * 2**consecutive, _DB_ERROR_BACKOFF_CAP_SECONDS
        )
        from .platform.ops_alerts import AlertCategory, Severity, emit

        emit(
            Severity.WARNING,
            AlertCategory.WORKER_LIFECYCLE,
            "Worker tick failed with a database error; retrying",
            details={
                "owner": self.owner,
                "error": type(exc.orig or exc).__name__,
                "consecutive_errors": consecutive,
                "retry_in_seconds": round(delay, 2),
            },
        )
        await asyncio.sleep(delay)

    @runs_as(WORKER_ROLE)
    async def run_once(self, *, job_types=None, exclude_types=()) -> bool:
        if job_types is None and self._onboarding_http_client is not None:
            from .generation.onboarding_dispatcher import (
                dispatch_one_onboarding_fact,
            )

            if await dispatch_one_onboarding_fact(
                self.owner,
                self._onboarding_http_client,
            ):
                return True
        if job_types is None:
            # Busy or idle: a queue that never drains must not starve the
            # schedules (#101 Q4).
            await self._dispatch_observation_ticks_if_due()
        job = await asyncio.to_thread(jobs.claim_next, self.owner,
            **({'job_types': job_types, 'exclude_types': exclude_types} if job_types is not None or exclude_types else {}))
        if job is None:
            # An idle tick is the reconciliation window. On PostgreSQL the
            # dispatcher retires epoch-stale and lease-expired Jobs inside
            # `marketai_claim_next_job_v2`, with no Python transition to fold
            # their Operation from -- so those Operations are folded here,
            # exactly when there is no queued work competing for the row lock.
            if job_types is None:
                await asyncio.to_thread(jobs.reconcile_admitted_operations)
                # Legacy Operations only their handlers settle (#22): fold
                # the ones whose Job ended without that handler ever running.
                from . import legacy_operations

                await asyncio.to_thread(legacy_operations.reconcile_legacy_operations_if_due)
            return False
        if job.type == "meta_read_ingestion":
            await self._meta_read_ingestion(job)
            return True
        if job.type == "launch_canary":
            await self._launch_canary(job)
            return True
        if job.type == "launch_provisioning":
            self._launch_provisioning(job)
            return True
        if job.type == "launch_recovery":
            self._launch_recovery(job)
            return True
        if job.type == "meta_schedule_tick":
            self._meta_schedule_tick(job)
            return True
        if job.type == "meta_observation_tick":
            self._meta_observation_tick(job)
            return True
        if job.type == "meta_observation_sync":
            self._meta_observation_sync(job)
            return True
        if job.type == "verify_upload_intent":
            await self._verify_upload_intent(job)
            return True
        if job.type == "cleanup_staging_upload":
            await self._cleanup_staging_upload(job)
            return True
        if job.type == "generation.run_item":
            await self._generation_run_item(job)
            return True
        if job.type == 'marketing.resolve_call':
            await self._marketing_resolution(job)
            return True
        if job.type == "marketing.image_edit":
            await self._marketing_image_edit(job)
            return True
        if job.type in ("marketing.plan", "marketing.ai_qc", "marketing.export"):
            handler = {"marketing.plan": self._marketing_plan, "marketing.ai_qc": self._marketing_ai_qc, "marketing.export": self._marketing_export}[job.type]
            await handler(job)
            return True
        if job.type == "generation.qc_asset_version":
            await self._generation_qc_asset_version(job)
            return True
        if job.type == "generation.export_package":
            await self._generation_export_package(job)
            return True
        if job.type == "library.connection_check":
            self._library_connection_check(job)
            return True
        if job.type == "library.manual_fact_import":
            await self._library_manual_fact_import(job)
            return True
        if job.type == "library.reference_gate":
            await self._library_reference_gate(job)
            return True
        if job.type == "products.intake":
            await self._products_intake(job)
            return True
        if job.type == "test_plans.draft":
            await self._test_plan_draft(job)
            return True
        if job.type == "external_creatives.process":
            await self._external_creative(job)
            return True
        if job.type == "competitor.resolve":
            from .competitors.resolve_worker import handle_resolve_job

            await handle_resolve_job(self, job)
            return True
        if job.type == "competitor_scan.run":
            from .competitors.scan_worker import handle_scan_job

            await handle_scan_job(self, job)
            return True
        if job.type == "scale_evaluate":
            self._scale_evaluate(job)
            return True
        if job.type == "scale_advice_expire":
            self._scale_advice_expire(job)
            return True
        if job.type == "scale_collect_sources":
            self._scale_collect_sources(job)
            return True
        if job.type == "scale_export":
            self._scale_export(job)
            return True
        if job.type in _LOCALIZATION_JOB_TYPES:
            await self._localization_job(job)
            return True
        if job.type != "generate_asset":
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="unknown_job_type",
                detail=f"未知任务类型: {job.type}",
                retryable=False,
            )
            return True
        await self._generate_asset(job)
        return True

    @runs_as(WORKER_ROLE)
    async def _localization_job(self, job: jobs.ClaimedJob) -> None:
        """Own the queue lifecycle around one Localization domain handler.

        The handlers themselves own their CAS transitions, provider-attempt
        evidence and downstream Job enqueues.  This fence owns only the
        shared queue lease.  Retrying an unexpected handler failure is safe:
        every external call re-enters ``ProviderCallCoordinator`` by the same
        Job/idempotency key, which replays terminal evidence and turns an
        ambiguous dispatch into ``uncertain`` instead of sending it again.

        A handler that returns normally succeeded.  Anything else it raises a
        :class:`HandlerOutcomeSignal` for, and
        :func:`settle_job_outcome` maps that onto the Job lifecycle -- which
        is what keeps a failed or ambiguous provider call from settling as a
        ``succeeded`` Job and folding its Operation to ``succeeded``.
        """

        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            await self._dispatch_localization_job(job)
        except HandlerOutcomeSignal as signal:
            settle_job_outcome(job.context, job.id, self.owner, signal)
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed payload will be malformed again next time.
            settle_job_outcome(
                job.context,
                job.id,
                self.owner,
                JobFailed("localization_invalid_job_payload", str(exc)),
            )
        except Exception as exc:
            # An unexpected fault is assumed transient: every external call
            # re-enters the coordinator by the same Job/idempotency key, so a
            # retry replays terminal evidence rather than sending again.
            settle_job_outcome(
                job.context,
                job.id,
                self.owner,
                JobRetry("localization_job_failed", str(exc)),
            )
        else:
            settle_job_outcome(job.context, job.id, self.owner, None)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @runs_as(WORKER_ROLE)
    async def _dispatch_localization_job(self, job: jobs.ClaimedJob) -> None:
        from .localization import worker as localization_worker

        def required_text(key: str) -> str:
            value = str(job.payload.get(key) or "")
            if not value:
                raise ValueError(f"missing {key}")
            return value

        batch_id = required_text("batch_id")

        if job.type == "localization.analyze_source":
            await localization_worker.handle_analyze_source(
                job.context,
                batch_id=batch_id,
                asset_version_id=required_text("asset_version_id"),
                analysis_recipe_hash=required_text("analysis_recipe_hash"),
                session_factory=tenant_session,
                coordinator=self._localization_coordinator,
            )
            return

        if job.type == "localization.translate_copy":
            await localization_worker.handle_translate_copy(
                job.context,
                batch_id=batch_id,
                asset_version_id=required_text("asset_version_id"),
                target_market=required_text("target_market"),
                inventory_id=required_text("inventory_id"),
                claim_decision_set_id=required_text("claim_decision_set_id"),
                session_factory=tenant_session,
                coordinator=self._localization_coordinator,
            )
            return

        if job.type == "localization.edit_attempt":
            attempt_number = int(job.payload.get("attempt_number") or 0)
            if attempt_number < 1:
                raise ValueError("missing or invalid attempt_number")
            await localization_worker.handle_edit_attempt(
                job.context,
                batch_id=batch_id,
                item_id=self._localization_resource_id(job),
                attempt_number=attempt_number,
                session_factory=tenant_session,
                coordinator=self._localization_coordinator,
            )
            return

        if job.type in {"localization.ocr_check", "localization.visual_qc"}:
            # Two admission paths produce these Jobs and they are scoped
            # differently. `confirm()` scopes them to the Item, so
            # `resource_id` *is* the Item. `authorize_isolated_qc()` scopes
            # them to the Attempt being recovered, so `resource_id` is an
            # Attempt id -- passing that as `item_id` looks up an Item that
            # does not exist and the recovery silently never runs. The
            # payload carries the identifiers explicitly for exactly this
            # reason; `resource_id` is only the fallback for the Item-scoped
            # form, whose payload predates that.
            item_id = str(job.payload.get("item_id") or "")
            attempt_id = str(job.payload.get("attempt_id") or "")
            if not item_id:
                item_id = self._localization_resource_id(job)
            if not attempt_id:
                attempt_number = int(job.payload.get("attempt_number") or 0)
                if attempt_number < 1:
                    raise ValueError("missing or invalid attempt_number")
                attempt_id = self._localization_attempt_id(
                    job, batch_id=batch_id, item_id=item_id,
                    attempt_number=attempt_number,
                )
            handler = (
                localization_worker.handle_ocr_check
                if job.type == "localization.ocr_check"
                else localization_worker.handle_visual_qc
            )
            with tenant_session(job.context) as session:
                handler(
                    session,
                    job.context,
                    batch_id=batch_id,
                    item_id=item_id,
                    attempt_id=attempt_id,
                    isolated=bool(job.payload.get("isolated")),
                )
            return

        if job.type == "localization.run_deterministic_qc":
            with tenant_session(job.context) as session:
                localization_worker.handle_run_deterministic_qc(
                    session,
                    job.context,
                    batch_id=batch_id,
                    item_id=required_text("item_id"),
                    attempt_id=required_text("attempt_id"),
                    isolated=bool(job.payload.get("isolated")),
                )
            return

        if job.type == "localization.finalize_item":
            with tenant_session(job.context) as session:
                localization_worker.handle_finalize_item(
                    session,
                    job.context,
                    batch_id=batch_id,
                    item_id=required_text("item_id"),
                    attempt_id=required_text("attempt_id"),
                )
            return

        if job.type == "localization.fold_batch":
            with tenant_session(job.context) as session:
                localization_worker.handle_fold_batch(
                    session, job.context, batch_id=batch_id
                )
            return

        raise ValueError(f"unsupported Localization job type: {job.type}")

    @staticmethod
    def _localization_resource_id(job: jobs.ClaimedJob) -> str:
        """Load the Item id omitted by the PostgreSQL claim function.

        Only valid for Item-scoped Jobs. Attempt-scoped Jobs (the ones
        `authorize_isolated_qc()` admits) carry their identifiers in the
        payload instead -- their `resource_id` is an Attempt.
        """

        from .adapters.persistence.models import JobRecord

        with tenant_session(job.context) as session:
            row = session.get(JobRecord, job.id)
            if row is None or row.workspace_id != job.context.workspace_id:
                raise ValueError(f"job {job.id!r} no longer exists")
            resource_id = str(row.resource_id or "")
        if not resource_id:
            raise ValueError("missing localization Job resource_id")
        return resource_id

    @staticmethod
    def _localization_attempt_id(
        job: jobs.ClaimedJob,
        *,
        batch_id: str,
        item_id: str,
        attempt_number: int,
    ) -> str:
        """Resolve pre-admitted external-QC Jobs to their Attempt row."""

        from .adapters.persistence.models import LocalizationAttemptRecord

        with tenant_session(job.context) as session:
            attempt = (
                session.query(LocalizationAttemptRecord)
                .filter_by(
                    workspace_id=job.context.workspace_id,
                    batch_id=batch_id,
                    item_id=item_id,
                    attempt_number=attempt_number,
                )
                .one_or_none()
            )
            attempt_id = str(attempt.id) if attempt is not None else ""
        if not attempt_id:
            raise ValueError(
                f"missing Localization attempt {item_id}:{attempt_number}"
            )
        return attempt_id

    @runs_as(WORKER_ROLE)
    async def _verify_upload_intent(self, job: jobs.ClaimedJob) -> None:
        """Job handler for spec §7.8's Worker verification sequence.

        The actual sequence lives in ``app.platform.upload_intents.verify_upload_intent``
        (steps 1-11); this method only translates its outcome into the
        Job/Operation lifecycle -- ``jobs.succeed``/``jobs.fail`` and the
        matching ``OperationState`` transition.
        """
        context = job.context
        intent_id = str(job.payload.get("intent_id") or "")
        operation_id = str(job.payload.get("operation_id") or "")
        if not intent_id or not operation_id:
            jobs.fail(
                context, job.id, self.owner,
                code="missing_input", detail="缺少 intent_id 或 operation_id", retryable=False,
            )
            return

        jobs.mark_running(context, job.id, self.owner)
        with tenant_session(context) as session:
            operation = get_operation(session, operation_id)
            # A retried attempt (after a transient storage error released
            # the lease) finds the Operation already `running` -- only the
            # first attempt makes the `queued -> running` transition.
            if operation.state == OperationState.QUEUED.value:
                transition_operation(
                    session, operation_id, OperationState.RUNNING, reason=None, actor=context.executor
                )

        # Hostnames are unbounded; the persisted verification lease is varchar(64).
        # Hash the full worker/job identity rather than silently truncating it.
        import hashlib
        lease_id = hashlib.sha256(f"{self.owner}:{job.id}".encode()).hexdigest()
        try:
            outcome = await upload_intents.verify_upload_intent(
                context,
                intent_id,
                session_factory=lambda: tenant_session(context),
                blob_store=self.blob_store,
                admission_reader=self.admission_reader,
                lease_id=lease_id,
            )
        except (upload_intents.UploadIntentLeaseLostError, upload_intents.UploadIntentNotFoundError) as exc:
            code = (
                "upload_intent_not_found"
                if isinstance(exc, upload_intents.UploadIntentNotFoundError)
                else "upload_intent_not_finalizable"
            )
            with tenant_session(context) as session:
                transition_operation(
                    session, operation_id, OperationState.FAILED, reason=code, actor=context.executor
                )
            jobs.fail(context, job.id, self.owner, code=code, detail=str(exc), retryable=False)
            return

        if outcome.kind == "finalized":
            committed = outcome.committed
            assert committed is not None
            with tenant_session(context) as session:
                transition_operation(
                    session,
                    operation_id,
                    OperationState.SUCCEEDED,
                    reason=None,
                    actor=context.executor,
                    result_ref=OperationResultRef(
                        resource_type="committed_blob", resource_id=committed.committed_key
                    ),
                )
            jobs.succeed(context, job.id, self.owner)
            return

        if outcome.kind == "rejected":
            with tenant_session(context) as session:
                transition_operation(
                    session,
                    operation_id,
                    OperationState.FAILED,
                    reason=outcome.rejection_code,
                    actor=context.executor,
                )
            jobs.fail(
                context,
                job.id,
                self.owner,
                code=outcome.rejection_code or "upload_verification_rejected",
                detail=outcome.rejection_code or "rejected",
                retryable=False,
            )
            return

        # kind == "retry": a transient BlobStore error released the CAS
        # lease back to `pending` (spec §7.9) -- the Operation stays
        # `running` and the next Job attempt re-acquires the lease.
        jobs.fail(
            context,
            job.id,
            self.owner,
            code="blob_store_transient_error",
            detail=outcome.detail,
            retryable=True,
        )

    @runs_as(WORKER_ROLE)
    async def _cleanup_staging_upload(self, job: jobs.ClaimedJob) -> None:
        """Delete a terminal UploadIntent's staging object idempotently.

        The Job is marked safe to retry after a Worker crash when it is
        enqueued.  ``BlobStore.delete`` treats an absent object as success,
        so retries converge without lifecycle support from the bucket.
        """
        staging_key = str(job.payload.get("staging_key") or "")
        if not staging_key:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="missing_input",
                detail="missing staging_key",
                retryable=False,
            )
            return

        jobs.mark_running(job.context, job.id, self.owner)
        not_before_text = str(job.payload.get("not_before") or "")
        if not_before_text:
            try:
                not_before = datetime.fromisoformat(not_before_text)
                if not_before.tzinfo is None:
                    not_before = not_before.replace(tzinfo=timezone.utc)
            except ValueError:
                jobs.fail(
                    job.context,
                    job.id,
                    self.owner,
                    code="invalid_cleanup_deadline",
                    detail="invalid not_before",
                    retryable=False,
                )
                return
            remaining = (not_before - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                jobs.fail(
                    job.context,
                    job.id,
                    self.owner,
                    code="staging_capability_still_live",
                    detail="cleanup deferred until upload capability expiry",
                    retryable=True,
                    retry_after_seconds=remaining,
                )
                return
        try:
            await self.blob_store.delete(BlobRef(key=staging_key))
        except BlobStoreError as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="staging_cleanup_failed",
                detail=str(exc),
                retryable=True,
            )
            return
        except Exception as exc:
            # Never persist a transport exception string: SDK exceptions may
            # contain endpoint URLs or request headers.
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="staging_cleanup_failed",
                detail=type(exc).__name__,
                retryable=True,
            )
            return
        jobs.succeed(job.context, job.id, self.owner)

    @runs_as(WORKER_ROLE)
    async def _marketing_resolution(self, job: jobs.ClaimedJob) -> None:
        from .marketing.recovery import execute_resolution, fail_resolution
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            await asyncio.to_thread(execute_resolution, job.context, str(job.payload['operation_id']))
            jobs.succeed(job.context, job.id, self.owner)
        except Exception:
            state = jobs.fail(job.context, job.id, self.owner, code='marketing_reconciliation_failed',
                detail='核对未完成，原调用和费用记录保留。', retryable=True)
            if state == 'failed':
                await asyncio.to_thread(fail_resolution, job.context, str(job.payload['operation_id']))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @runs_as(WORKER_ROLE)
    async def _marketing_ai_qc(self, job: jobs.ClaimedJob) -> None:
        from .marketing.ai_qc_worker import execute_ai_qc
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            await execute_ai_qc(job.context, str(job.payload.get("call_id", "")), self.blob_store)
            jobs.succeed(job.context, job.id, self.owner)
        except Exception:
            jobs.fail(job.context, job.id, self.owner, code="marketing_ai_qc_failed",
                detail="AI 检查未完成，请查看检查状态；不会自动重复付费调用。", retryable=False)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @runs_as(WORKER_ROLE)
    async def _marketing_image_edit(self, job: jobs.ClaimedJob) -> None:
        from .marketing.image_editing import execute_edit, fail_edit
        from .marketing.service import MarketingError
        from .generation.selection import SelectionError
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            await execute_edit(job.context, str(job.payload["edit_id"]), self.blob_store)
            jobs.succeed(job.context, job.id, self.owner)
        except Exception as exc:
            state = jobs.fail(job.context, job.id, self.owner, code="image_edit_failed",
                detail="图片修改未完成，原版本保留。", retryable=not isinstance(exc, (MarketingError, SelectionError)))
            if state == "failed":
                await asyncio.to_thread(fail_edit, job.context, str(job.payload["edit_id"]))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @runs_as(WORKER_ROLE)
    async def _marketing_export(self, job: jobs.ClaimedJob) -> None:
        from .marketing.handoff_worker import execute_export
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            await execute_export(job.context, str(job.payload.get("handoff_id", "")), self.blob_store)
            jobs.succeed(job.context, job.id, self.owner)
        except Exception:
            jobs.fail(job.context, job.id, self.owner, code="marketing_export_failed",
                      detail="导出未完成，请查看导出包状态。", retryable=False)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @runs_as(WORKER_ROLE)
    async def _marketing_plan(self, job: jobs.ClaimedJob) -> None:
        from .marketing.planner import execute_planning

        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            await execute_planning(job.context, str(job.payload.get("call_id", "")))
            jobs.succeed(job.context, job.id, self.owner)
        except Exception:
            jobs.fail(job.context, job.id, self.owner, code="marketing_planning_failed",
                      detail="策划任务未完成，请查看任务状态；不会自动重复付费调用。", retryable=False)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @runs_as(WORKER_ROLE)
    async def _generation_run_item(self, job: jobs.ClaimedJob) -> None:
        """Dispatch fence for Generation Worker handler.

        Two paths:

        * ``execution_mode == "mock"`` (Issue #21 Task 3's original path):
          ``handle_run_item`` does its own CAS + ProviderAttempt bookkeeping
          inside a single ``tenant_session``.
        * ``execution_mode == "controlled_mock"`` (Issue #98 Task 1):
          ``handle_run_item_coordinated`` uses the three-phase
          ``ExternalCallCoordinator``, proving the SIGKILL-safe seam end to
          end.
        * ``execution_mode == "byok"``: ``handle_run_item_byok`` dispatches
          the same coordinated way against the real provider with the
          Workspace's own credential and spend reservation.
        """
        run_item_id = str(job.payload.get("run_item_id") or "")
        if not run_item_id:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="missing_input",
                detail="缺少 run_item_id",
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)

        # Check execution mode to decide which path to use.
        execution_mode: str | None = None
        try:
            with tenant_session(job.context) as session:
                from .generation.models import RunItemRecord
                from sqlalchemy import select as sa_select
                item = session.execute(
                    sa_select(RunItemRecord).where(
                        RunItemRecord.id == run_item_id,
                        RunItemRecord.workspace_id == job.context.workspace_id,
                    )
                ).scalar_one()
                execution_mode = item.execution_mode
                from .generation.models import GenerationRecipeSnapshotRecord
                from .adapters.persistence.models import GenerationRunRecord
                run = session.get(GenerationRunRecord, item.generation_run_id)
                snapshot = session.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id)
                selected_recipe = "selection" in snapshot.canonical_json
        except Exception as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="generation_run_item_failed",
                detail=str(exc),
                retryable=False,
            )
            return

        if selected_recipe:
            heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
            try:
                from .generation.paid_execution import execute_run_item
                await execute_run_item(job.context, job.id, run_item_id, self.blob_store)
                jobs.succeed(job.context, job.id, self.owner)
            except Exception:
                from .generation.paid_execution import record_execution_failure
                record_execution_failure(job.context, job.id, run_item_id)
                jobs.fail(job.context, job.id, self.owner, code="marketing_generation_failed", detail="生成未完成，请查看任务状态。", retryable=False)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass
            return
        if execution_mode == "controlled_mock":
            await self._generation_run_item_coordinated(job, run_item_id)
        elif execution_mode == "byok":
            await self._generation_run_item_byok(job, run_item_id)
        else:
            await self._generation_run_item_legacy(job, run_item_id)

    async def _generation_run_item_legacy(
        self, job: jobs.ClaimedJob, run_item_id: str
    ) -> None:
        """Original single-session path for ``execution_mode == "mock"``."""
        from .generation.thumbnails import commit_thumbnail_best_effort
        from .generation.worker import handle_run_item, materialize_mock_output

        try:
            with tenant_session(job.context) as session:
                from .generation.models import RunItemRecord

                item = session.execute(
                    select(RunItemRecord).where(
                        RunItemRecord.id == run_item_id,
                        RunItemRecord.workspace_id == job.context.workspace_id,
                    )
                ).scalar_one()
                semantic_fingerprint = item.semantic_fingerprint

            output, committed_blob_ref = await materialize_mock_output(
                self.blob_store,
                job.context,
                semantic_fingerprint,
            )
            # The gallery thumbnail is derived from the same bytes and
            # committed beside the original; a failure here is logged and
            # leaves the AssetVersion without one -- never a failed item.
            committed_thumbnail = await commit_thumbnail_best_effort(
                self.blob_store,
                job.context.workspace_id,
                output.get("content_bytes"),
                log_context={"run_item_id": run_item_id, "execution_mode": "mock"},
            )
            with tenant_session(job.context) as session:
                handle_run_item(
                    session,
                    job.context,
                    run_item_id,
                    output_override=output,
                    committed_blob_ref=committed_blob_ref,
                    committed_thumbnail=committed_thumbnail,
                )
            jobs.succeed(job.context, job.id, self.owner)
        except Exception as exc:
            self._fail_run_item_after_handler_fault(job, run_item_id, exc)
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="generation_run_item_failed",
                detail=str(exc),
                retryable=False,
            )

    def _fail_run_item_after_handler_fault(
        self, job: jobs.ClaimedJob, run_item_id: str, exc: BaseException
    ) -> None:
        """Project a handler fault onto the RunItem before the Job settles.

        The Job's ``failed`` state is invisible to the Run page; the RunItem's
        is not. Best effort: a second failure here (the item is gone, or a
        concurrent command moved it) must not mask the original fault, which
        ``jobs.fail`` records right after.
        """
        from .generation.worker import fail_run_item_after_handler_fault

        summary = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        try:
            with tenant_session(job.context) as session:
                fail_run_item_after_handler_fault(
                    session,
                    job.context,
                    run_item_id,
                    error_summary=f"generation_run_item_failed: {summary}"[:1000],
                )
        except Exception:
            logger.exception(
                "could not project the handler fault onto RunItem %s", run_item_id
            )

    @runs_as(WORKER_ROLE)
    async def _generation_qc_asset_version(self, job: jobs.ClaimedJob) -> None:
        """Quality-check the exact generated AssetVersion/RunItem (Issue #169).

        ``run_quality_check`` owns the governed VLM call (reserved by the
        command that admitted the item, settled here at usage), the check
        rows, the RunItem transition and the human-quality projection. A
        recipe frozen before #169 keeps the deterministic check.
        """
        from .generation.qc import record_quality_check_fault, run_quality_check

        asset_version_id = str(job.payload.get("asset_version_id") or "")
        run_item_id = str(job.payload.get("run_item_id") or "")
        if not asset_version_id or not run_item_id:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="missing_input",
                detail="缺少 asset_version_id 或 run_item_id",
                retryable=False,
            )
            return

        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            outcome = await run_quality_check(
                job.context,
                asset_version_id=asset_version_id,
                run_item_id=run_item_id,
                llm_client=self.llm_chat_client(),
                blob_store=self.blob_store,
                session_factory=tenant_session,
            )
            logger.info(
                "generation_qc_recorded",
                extra={
                    "workspace_id": job.context.workspace_id,
                    "asset_version_id": asset_version_id,
                    "qc_attempt_id": outcome.attempt_id,
                    "state": outcome.state,
                    "consecutive_failures": outcome.consecutive_failures,
                },
            )
            jobs.succeed(job.context, job.id, self.owner)
        except Exception as exc:
            logger.exception(
                "generation_qc_failed",
                extra={"workspace_id": job.context.workspace_id, "asset_version_id": asset_version_id},
            )
            record_quality_check_fault(
                job.context,
                asset_version_id=asset_version_id,
                run_item_id=run_item_id,
                error=exc,
                session_factory=tenant_session,
            )
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="generation_qc_failed",
                detail=str(exc)[:1000],
                retryable=False,
            )
        finally:
            heartbeat.cancel()

    async def _generation_run_item_coordinated(
        self, job: jobs.ClaimedJob, run_item_id: str,
    ) -> None:
        """Three-phase coordinated path for ``controlled_mock``."""
        from .generation.controlled_adapter import ControlledGenerationAdapter
        from .generation.evidence import GenerationEvidenceRecorder
        from .generation.worker import handle_run_item_coordinated

        coordinator = self._localization_coordinator
        adapter = ControlledGenerationAdapter()
        evidence_recorder = GenerationEvidenceRecorder(run_item_id)

        try:
            await handle_run_item_coordinated(
                coordinator,
                job.context,
                run_item_id,
                adapter=adapter,
                evidence_recorder=evidence_recorder,
                session_factory=tenant_session,
            )
            jobs.succeed(job.context, job.id, self.owner)
        except Exception as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="generation_run_item_coordinated_failed",
                detail=str(exc),
                retryable=False,
            )

    def _byok_adapters(self) -> dict:
        """The real-provider adapters this Worker can dispatch ``byok`` items to."""
        adapters = getattr(self, "_byok_adapter_registry", None)
        if adapters is None:
            from .generation.gemini_adapter import GeminiImageAdapter
            from .generation.openai_adapter import OpenAIImageAdapter
            from .generation.seedream_adapter import SeedreamImageAdapter

            # Every provider ``capabilities.PLATFORM_ADAPTER_REGISTRY``
            # advertises under ``byok`` (Issue #167 reconnected Gemini and
            # Seedream to the governed path).
            adapters = {
                "openai": OpenAIImageAdapter(),
                "gemini": GeminiImageAdapter(),
                "seedream": SeedreamImageAdapter(),
            }
            self._byok_adapter_registry = adapters
        return adapters

    def llm_chat_client(self):
        """The governed ``llm_chat`` client job handlers call (Issue #160).

        Same coordinator, credential protector and BlobStore as BYOK image
        dispatch: calls are durable ProviderCallAttempts, paid with the
        Workspace's own bound OpenAI key against the reservation the
        admitting command made, or answered by the controlled mock.
        """
        client = getattr(self, "_llm_chat_client", None)
        if client is None:
            from .entitlements.credential_protector import LocalKmsAdapter
            from .providers.llm_chat import LlmChatClient

            client = LlmChatClient(
                coordinator=self._localization_coordinator,
                session_factory=tenant_session,
                protector=LocalKmsAdapter.local_only(),
                blob_store=self.blob_store,
            )
            self._llm_chat_client = client
        return client

    async def _generation_run_item_byok(
        self, job: jobs.ClaimedJob, run_item_id: str,
    ) -> None:
        """Three-phase coordinated path for ``byok`` (real provider, own key)."""
        from .entitlements.credential_protector import LocalKmsAdapter
        from .generation.byok_dispatch import handle_run_item_byok

        try:
            await handle_run_item_byok(
                self._localization_coordinator,
                job.context,
                run_item_id,
                adapters=self._byok_adapters(),
                blob_store=self.blob_store,
                protector=LocalKmsAdapter.local_only(),
                session_factory=tenant_session,
            )
            jobs.succeed(job.context, job.id, self.owner)
        except Exception as exc:
            self._fail_run_item_after_handler_fault(job, run_item_id, exc)
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="generation_run_item_byok_failed",
                detail=str(exc),
                retryable=False,
            )

    @runs_as(WORKER_ROLE)
    async def _generation_export_package(self, job: jobs.ClaimedJob) -> None:
        """Dispatch fence for Issue #49 Task 9's export Worker handler.

        ``handle_export_package`` owns its own ExportPackage/Operation
        transitions (``pending`` -> ``building`` -> ``ready``/``failed``,
        mirroring the Operation into ``running``/``succeeded``/``failed``);
        this method only owns the Job lifecycle around it, the same division
        of responsibility ``_generation_run_item`` uses for the RunItem
        Worker.
        """
        from .generation.export_worker import handle_export_package

        package_id = str(job.payload.get("package_id") or "")
        if not package_id:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="missing_input",
                detail="缺少 package_id",
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(job.context) as session:
                state = await handle_export_package(session, self.blob_store, package_id)
        except Exception as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="generation_export_package_failed",
                detail=str(exc),
                retryable=False,
            )
            return
        # The build and the package's terminal state commit together, and a
        # rerun of a Job whose package already settled (#22) closes the Job
        # the same way the run that settled it would have.
        if state == "ready":
            jobs.succeed(job.context, job.id, self.owner)
            return
        jobs.fail(
            job.context,
            job.id,
            self.owner,
            code="generation_export_package_failed",
            detail=f"导出包已是 {state}",
            retryable=False,
        )

    @runs_as(WORKER_ROLE)
    def _library_connection_check(self, job: jobs.ClaimedJob) -> None:
        """Run the local DataSource check (#383, ``app.library.connection_checks``).

        A finished check is ``succeeded`` whatever it concluded, its audit
        event the result; only a check that could not run fails. The
        evidence and the Operation settle in one transaction, so a rerun of
        a Job whose Operation already settled only closes the Job.
        """
        from .library import connection_checks

        operation_id = str(job.payload.get("operation_id") or "")
        data_source_entry_id = str(job.payload.get("data_source_entry_id") or "")
        requested = job.payload.get("execution_mode")
        requested_mode = str(requested) if requested else None
        if not operation_id or not data_source_entry_id:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail="缺少 operation_id 或 data_source_entry_id", retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        with tenant_session(job.context) as session:
            operation = get_operation(session, operation_id)
            settled = OperationState(operation.state) in TERMINAL_STATES
            if operation.state == OperationState.QUEUED.value:
                transition_operation(
                    session, operation_id, OperationState.RUNNING, reason=None, actor=job.context.executor
                )
        if settled:
            jobs.succeed(job.context, job.id, self.owner)
            return
        try:
            with tenant_session(job.context) as session:
                evidence = connection_checks.record_connection_check(
                    session,
                    data_source_entry_id=data_source_entry_id,
                    requested_mode=requested_mode,
                    operation_id=operation_id,
                )
                transition_operation(
                    session, operation_id, OperationState.SUCCEEDED,
                    reason=None, actor=job.context.executor,
                    result_ref=OperationResultRef(
                        resource_type=connection_checks.RESULT_RESOURCE_TYPE,
                        resource_id=evidence.id,
                    ),
                )
        except Exception as exc:
            logger.warning(
                "connection check could not run: operation=%s data_source=%s error=%s",
                operation_id, data_source_entry_id, type(exc).__name__,
            )
            with tenant_session(job.context) as session:
                transition_operation(
                    session, operation_id, OperationState.FAILED,
                    reason="connection_check_failed", actor=job.context.executor,
                )
            jobs.fail(
                job.context, job.id, self.owner,
                code="connection_check_failed", detail=str(exc), retryable=False,
            )
            return
        jobs.succeed(job.context, job.id, self.owner)

    @runs_as(WORKER_ROLE)
    async def _library_manual_fact_import(self, job: jobs.ClaimedJob) -> None:
        """Import a finalized upload into a new Manual Fact Set draft (#397).

        The draft and the Operation's ``succeeded`` commit in one transaction
        under the Operation's row lock, so a crash leaves both or neither and
        the Job is safe to requeue: a rerun finds the Operation settled and
        only closes the Job, or imports from scratch. Two runs of one Job
        (a lease expired mid-import) serialize on the lock, and the second
        finds the Operation no longer ``running`` and writes nothing.
        """
        from .library.manual_fact_import import (
            ManualFactImportError,
            read_manual_fact_payload,
            write_manual_fact_set,
        )
        from .platform.blob_store import CommittedBlobRef

        operation_id = str(job.payload.get("operation_id") or "")
        data_source_entry_id = str(job.payload.get("data_source_entry_id") or "")
        upload_intent_id = str(job.payload.get("upload_intent_id") or "")
        # Jobs queued before #54 carry no default Offer.
        default_offer_entry_id = job.payload.get("default_offer_entry_id") or None
        if not operation_id or not data_source_entry_id or not upload_intent_id:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail="缺少必要参数", retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        with tenant_session(job.context) as session:
            operation = get_operation(session, operation_id)
            settled = operation.state if OperationState(operation.state) in TERMINAL_STATES else None
            if operation.state == OperationState.QUEUED.value:
                transition_operation(
                    session, operation_id, OperationState.RUNNING, reason=None, actor=job.context.executor
                )
        if settled is not None:
            self._close_settled_fact_import(job, settled)
            return
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            with tenant_session(job.context) as session:
                intent = upload_intents.get_upload_intent(session, upload_intent_id)
                if intent.state != "finalized":
                    raise ManualFactImportError("Upload Intent 尚未完成验证")
                if intent.purpose != "library_manual_fact_import":
                    raise ManualFactImportError("Upload Intent purpose 不匹配")
                if (
                    intent.owner_kind != "data_source"
                    or intent.owner_id != data_source_entry_id
                ):
                    raise ManualFactImportError("Upload Intent 所属数据源不匹配")
                if not intent.committed_blob_ref_key or not intent.committed_blob_ref_version:
                    raise ManualFactImportError("Upload Intent 缺少已提交对象引用")

                if intent.committed_blob_ref_key.endswith(".json"):
                    verified_mime = "application/json"
                elif intent.committed_blob_ref_key.endswith(".csv"):
                    verified_mime = "text/csv"
                else:
                    raise ManualFactImportError("Upload Intent 已提交对象类型不受支持")

                committed_ref = CommittedBlobRef(
                    committed_key=intent.committed_blob_ref_key,
                    storage_version_id=intent.committed_blob_ref_version,
                    size_bytes=intent.content_length_max,
                    checksum_sha256_b64=intent.checksum_sha256_b64,
                    content_type_verified=verified_mime,
                )

            content_payload = await read_manual_fact_payload(
                self.blob_store,
                committed_ref,
            )
            with tenant_session(job.context) as session:
                state = _locked_operation(session, job.context, operation_id).state
                if state != OperationState.RUNNING.value:
                    settled = state
                else:
                    fact_set = write_manual_fact_set(
                        session,
                        job.context,
                        data_source_entry_id=data_source_entry_id,
                        upload_intent_id=upload_intent_id,
                        content_payload=content_payload,
                        default_offer_entry_id=default_offer_entry_id,
                    )
                    transition_operation(
                        session, operation_id, OperationState.SUCCEEDED,
                        reason=None, actor=job.context.executor,
                        result_ref=OperationResultRef(
                            resource_type="manual_fact_set_draft",
                            resource_id=fact_set.id,
                        ),
                    )
        except Exception as exc:
            # A file the user has to fix is an expected outcome; anything
            # else is a fault worth a line in the log.
            if not isinstance(exc, ManualFactImportError):
                logger.warning(
                    "manual fact import could not run: operation=%s data_source=%s error=%s",
                    operation_id, data_source_entry_id, type(exc).__name__,
                )
            with tenant_session(job.context) as session:
                operation = _locked_operation(session, job.context, operation_id)
                if OperationState(operation.state) not in TERMINAL_STATES:
                    transition_operation(
                        session, operation_id, OperationState.FAILED,
                        reason="manual_fact_import_failed", actor=job.context.executor,
                    )
            jobs.fail(
                job.context, job.id, self.owner,
                code="manual_fact_import_failed", detail=str(exc), retryable=False,
            )
            return
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        if settled is not None:
            self._close_settled_fact_import(job, settled)
            return
        jobs.succeed(job.context, job.id, self.owner)

    def _close_settled_fact_import(self, job: jobs.ClaimedJob, state: str) -> None:
        """Close the Job the way another run already settled its Operation.

        A Job left ``succeeded`` beside a failed import would keep its upload
        from being imported again (``upload_intent_consumed``).
        """
        self._close_settled_job(job, state, code="manual_fact_import_failed")

    def _close_settled_job(self, job: jobs.ClaimedJob, state: str, *, code: str) -> None:
        """Close the Job the way another run already settled its Operation."""
        if state == OperationState.SUCCEEDED.value:
            jobs.succeed(job.context, job.id, self.owner)
            return
        jobs.fail(
            job.context, job.id, self.owner,
            code=code, detail=f"Operation 已是 {state}", retryable=False,
        )

    @runs_as(WORKER_ROLE)
    async def _library_reference_gate(self, job: jobs.ClaimedJob) -> None:
        """Run reference upload gate checks on a quarantined object.

        The route created the ReferenceObjectRecord with ``original_object_key``
        pointing at the committed blob (a ``BlobStore`` key in production, a
        local path on the dev sync-upload path). ``run_reference_gate`` reads
        it through ``self.blob_store``, evaluates every check, commits the
        controlled derivative back through the same adapter and records the
        evidence; this fence only maps its outcome onto the Job/Operation
        lifecycle.

        The content-safety execution mode is resolved from deployment
        configuration inside ``run_reference_gate`` -- the Job payload never
        carries it, so a client cannot pick the Mock (Issue #158).

        The Job is requeued after a crash (#22). A rerun finds the Operation
        already ``running`` and carries on, or finds it settled and only
        closes the Job; the object is decided under its own row lock and the
        Operation under its, so two runs of one Job each write once.
        """
        from .library import references
        from .library.reference_gate_worker import run_reference_gate

        operation_id = str(job.payload.get("operation_id") or "")
        object_id = str(job.payload.get("object_id") or "")
        if not operation_id or not object_id:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail="缺少 operation_id 或 object_id",
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        with tenant_session(job.context) as session:
            operation = _locked_operation(session, job.context, operation_id)
            settled = operation.state if OperationState(operation.state) in TERMINAL_STATES else None
            if operation.state == OperationState.QUEUED.value:
                transition_operation(
                    session, operation_id, OperationState.RUNNING,
                    reason=None, actor=job.context.executor,
                )
        if settled is not None:
            self._close_settled_job(job, settled, code="reference_gate_failed")
            return
        result_ref = OperationResultRef(
            resource_type="reference_object", resource_id=object_id,
        )
        try:
            outcome = await run_reference_gate(job.context, self.blob_store, object_id)
        except references.ReferenceGateError as exc:
            self._fail_reference_gate(
                job, operation_id, result_ref, code=exc.failure_code, detail=str(exc),
            )
            return
        except Exception as exc:
            self._fail_reference_gate(
                job, operation_id, result_ref, code="reference_gate_failed", detail=str(exc),
            )
            return

        if outcome.succeeded:
            settled = _settle_running_operation(
                job.context, operation_id, OperationState.SUCCEEDED,
                reason=None, result_ref=result_ref,
            )
            if settled is not None:
                self._close_settled_job(job, settled, code="reference_gate_failed")
                return
            jobs.succeed(job.context, job.id, self.owner)
            return

        # A transient storage read failure leaves the Operation running until
        # the Job's retry budget is spent; every other outcome is final.
        if outcome.retryable and job.attempt < job.max_attempts:
            jobs.fail(
                job.context, job.id, self.owner,
                code=outcome.code, detail=outcome.detail, retryable=True,
            )
            return
        self._fail_reference_gate(
            job, operation_id, result_ref, code=outcome.code, detail=outcome.detail,
        )

    def _fail_reference_gate(
        self, job: jobs.ClaimedJob, operation_id: str, result_ref: OperationResultRef,
        *, code: str, detail: str,
    ) -> None:
        """Settle the Operation ``failed``, then the Job -- in that order, so
        a crash between the two leaves a Job a rerun can close."""
        settled = _settle_running_operation(
            job.context, operation_id, OperationState.FAILED,
            reason=code, result_ref=result_ref,
        )
        if settled is not None:
            self._close_settled_job(job, settled, code=code)
            return
        jobs.fail(
            job.context, job.id, self.owner, code=code, detail=detail, retryable=False,
        )

    def intake_fetcher(self):
        """The SSRF-safe fetcher product intake reads pasted links with (Issue #161)."""
        fetcher = getattr(self, "_intake_fetcher", None)
        if fetcher is None:
            from .products.intake_fetch import SafeFetcher

            fetcher = SafeFetcher()
            self._intake_fetcher = fetcher
        return fetcher

    @runs_as(WORKER_ROLE)
    async def _products_intake(self, job: jobs.ClaimedJob) -> None:
        """Fence for the product-intake Job: fetch -> extract -> images.

        ``run_product_intake`` owns the steps and their evidence; this maps
        its outcome onto the Job, whose Operation (admitted with aggregation)
        folds from it: ``ready`` succeeds, a transient fetch failure retries
        while attempts remain, an ambiguous LLM call is ``uncertain`` (never
        retried blindly), every other outcome fails with the intake's code.
        """
        from .products.intake_worker import run_product_intake

        intake_id = str(job.payload.get("intake_id") or "")
        operation_id = str(job.payload.get("operation_id") or "")
        if not intake_id or not operation_id:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail="缺少 intake_id 或 operation_id", retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            outcome = await run_product_intake(
                job.context,
                intake_id=intake_id,
                job_id=job.id,
                operation_id=operation_id,
                fetcher=self.intake_fetcher(),
                llm_client=self.llm_chat_client(),
                blob_store=self.blob_store,
                final_attempt=job.attempt >= job.max_attempts,
            )
        except Exception as exc:  # noqa: BLE001 -- recorded on the intake, never a crash loop
            logger.exception(
                "product_intake_failed",
                extra={"workspace_id": job.context.workspace_id, "intake_id": intake_id},
            )
            from .products.intake_worker import mark_intake_failed

            mark_intake_failed(job.context, intake_id, code="internal_error", detail=type(exc).__name__)
            jobs.fail(
                job.context, job.id, self.owner,
                code="product_intake_failed", detail=str(exc)[:500], retryable=False,
            )
            return
        finally:
            heartbeat.cancel()
        if outcome.kind == "ready":
            jobs.succeed(job.context, job.id, self.owner)
        elif outcome.kind == "retry":
            jobs.fail(
                job.context, job.id, self.owner,
                code=outcome.code, detail=outcome.detail, retryable=True,
            )
        elif outcome.kind == "uncertain":
            jobs.mark_uncertain(
                job.context, job.id, self.owner, code=outcome.code, detail=outcome.detail,
            )
        else:
            jobs.fail(
                job.context, job.id, self.owner,
                code=outcome.code or "product_intake_failed", detail=outcome.detail, retryable=False,
            )

    @runs_as(WORKER_ROLE)
    async def _external_creative(self, job: jobs.ClaimedJob) -> None:
        """Fence for the external-creative Job (Issue #168): gate -> asset -> tag.

        ``run_external_creative`` owns the steps and their evidence; this maps
        its outcome onto the Job, whose aggregated Operation folds from it:
        ``ready`` succeeds (also when tagging failed -- the asset exists and
        the user tags by hand), a transient storage failure retries while
        attempts remain, an ambiguous paid tagging call is ``uncertain``
        (never resent blindly), a gate rejection or other fault fails.
        """
        from .external_creatives.worker import mark_failed, run_external_creative

        creative_id = str(job.payload.get("external_creative_id") or "")
        operation_id = str(job.payload.get("operation_id") or "")
        if not creative_id or not operation_id:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail="缺少 external_creative_id 或 operation_id", retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            outcome = await run_external_creative(
                job.context,
                creative_id=creative_id,
                job_id=job.id,
                operation_id=operation_id,
                llm_client=self.llm_chat_client(),
                blob_store=self.blob_store,
                final_attempt=job.attempt >= job.max_attempts,
            )
        except Exception as exc:  # noqa: BLE001 -- recorded on the creative, never a crash loop
            logger.exception(
                "external_creative_failed",
                extra={"workspace_id": job.context.workspace_id, "external_creative_id": creative_id},
            )
            mark_failed(job.context, creative_id, code="internal_error", detail=type(exc).__name__)
            jobs.fail(
                job.context, job.id, self.owner,
                code="external_creative_failed", detail=str(exc)[:500], retryable=False,
            )
            return
        finally:
            heartbeat.cancel()
        if outcome.kind == "ready":
            jobs.succeed(job.context, job.id, self.owner)
        elif outcome.kind == "retry":
            jobs.fail(
                job.context, job.id, self.owner,
                code=outcome.code, detail=outcome.detail, retryable=True,
            )
        elif outcome.kind == "uncertain":
            jobs.mark_uncertain(
                job.context, job.id, self.owner, code=outcome.code, detail=outcome.detail,
            )
        else:
            jobs.fail(
                job.context, job.id, self.owner,
                code=outcome.code or "external_creative_failed", detail=outcome.detail, retryable=False,
            )

    @runs_as(WORKER_ROLE)
    async def _test_plan_draft(self, job: jobs.ClaimedJob) -> None:
        """Fence for the test-plan drafting Job (Issues #163, #164).

        ``run_plan_drafting`` owns the step, its evidence and the plan's
        state (a ``byok`` plan drafts through the governed ``llm_chat``
        client); this maps the outcome onto the Job, whose aggregated
        Operation folds from it (``ready`` -> succeeded, an ambiguous paid
        call -> uncertain, otherwise failed). An unexpected fault fails the
        plan instead of crash-looping.
        """
        from .plans.worker import mark_plan_failed, run_plan_drafting

        plan_id = str(job.payload.get("plan_id") or "")
        if not plan_id:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail="缺少 plan_id", retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            outcome = await run_plan_drafting(
                job.context, plan_id=plan_id, llm_client=self.llm_chat_client(),
            )
        except Exception as exc:  # noqa: BLE001 -- recorded on the plan, never a crash loop
            logger.exception(
                "test_plan_draft_failed",
                extra={"workspace_id": job.context.workspace_id, "plan_id": plan_id},
            )
            mark_plan_failed(job.context, plan_id, code="internal_error", detail=type(exc).__name__)
            jobs.fail(
                job.context, job.id, self.owner,
                code="test_plan_draft_failed", detail=str(exc)[:500], retryable=False,
            )
            return
        finally:
            heartbeat.cancel()
        if outcome.kind == "ready":
            jobs.succeed(job.context, job.id, self.owner)
        elif outcome.kind == "uncertain":
            # A paid drafting call whose outcome is unknown: reconcile, never
            # resend blindly (Issue #164, same as product intake).
            jobs.mark_uncertain(
                job.context, job.id, self.owner, code=outcome.code, detail=outcome.detail,
            )
        else:
            jobs.fail(
                job.context, job.id, self.owner,
                code=outcome.code or "test_plan_draft_failed",
                detail=outcome.detail, retryable=False,
            )

    @runs_as(WORKER_ROLE)
    def _scale_evaluate(self, job: jobs.ClaimedJob) -> None:
        """Dispatch fence for the ``scale_evaluate`` job (spec §3.8, Issue #25
        Task 14). This shared Worker's ``run_scale_evaluation`` call is a pure
        delegation to ``app.scale.service`` -- Scale itself never touches this
        module's ``meta_client``/``launch_adapter``. This Worker is the one
        deployed, so it runs Scale jobs; ADR 0017 allows that only while the
        process has no Meta write capability, which ``_main`` checks at
        startup (``assert_no_meta_write_capability``). The capability-scoped
        ``app.scale.worker.ScaleWorkerRuntime`` is the runtime to split out
        once that no longer holds.

        The run's Meta mode comes from the payload (``evaluation_job_context``,
        #97): the claimed context names none, and RLS shows Scale rows only to
        a context that does. A payload without one fails as ``missing_input``.
        A run whose Episode the previous run still holds is deferred, not
        failed: the Job retries it (``ScaleEvaluationDeferred``, #110).
        """
        from app.scale.service import (
            ScaleEvaluationDeferred,
            ScaleOrchestrationError,
            evaluation_job_context,
            run_scale_evaluation,
        )

        evaluation_run_id = str(job.payload.get("evaluation_run_id") or "")
        try:
            if not evaluation_run_id:
                raise ScaleOrchestrationError("missing_input", "缺少 evaluation_run_id")
            context = evaluation_job_context(job.context, job.payload)
        except ScaleOrchestrationError as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code=exc.code,
                detail=str(exc),
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(context) as session:
                run_scale_evaluation(session, context, evaluation_run_id)
            jobs.succeed(job.context, job.id, self.owner)
        except ScaleEvaluationDeferred as exc:
            # Another run holds the Episode (#110): the run is untouched and
            # waits for it, it is not failed.
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code=exc.code,
                detail=str(exc),
                retryable=True,
                retry_after_seconds=exc.retry_after_seconds,
            )
        except ScaleOrchestrationError as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code=exc.code,
                detail=str(exc),
                retryable=False,
            )
        except Exception as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="scale_evaluate_failed",
                detail=str(exc),
                retryable=False,
            )

    @runs_as(WORKER_ROLE)
    def _scale_advice_expire(self, job: jobs.ClaimedJob) -> None:
        """Dispatch fence for the ``scale_advice_expire`` deadline job (spec
        §3.5, #97). Like ``_scale_evaluate`` it only delegates to
        ``app.scale`` and takes the thread's Meta mode from the payload;
        ``expire_advice_thread`` rechecks the deadline under the thread's row
        lock, so a rerun after a crash changes nothing twice.
        """
        from app.scale.advice import (
            AdviceLifecycleError,
            advice_expire_job_context,
            expire_advice_thread,
        )

        thread_id = str(job.payload.get("thread_id") or "")
        try:
            if not thread_id:
                raise AdviceLifecycleError("missing_input", "缺少 thread_id")
            context = advice_expire_job_context(job.context, job.payload)
        except AdviceLifecycleError as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code=exc.code,
                detail=str(exc),
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(context) as session:
                expire_advice_thread(session, context, thread_id)
            jobs.succeed(job.context, job.id, self.owner)
        except AdviceLifecycleError as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code=exc.code,
                detail=str(exc),
                retryable=False,
            )
        except Exception as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="scale_advice_expire_failed",
                detail=str(exc),
                retryable=False,
            )

    @runs_as(WORKER_ROLE)
    def _scale_collect_sources(self, job: jobs.ClaimedJob) -> None:
        """Dispatch fence for the ``scale_collect_sources`` job (#109, #101
        S3). Like ``_scale_evaluate`` it only delegates to ``app.scale``: the
        collector reads the observation batch's stored pages and parses them
        with ``app.meta``'s pure parsers; it resolves no adapter (ADR 0017).
        The Meta mode comes from the payload. Each Episode's manifest
        commits on its own and is found again by its collection key, so a
        retry after a crash or a partial failure writes nothing twice.
        """
        from app.scale.collector import (
            SourceCollectionError,
            SourceCollectionFailed,
            collection_job_input,
            run_source_collection,
        )

        try:
            request = collection_job_input(job.context, job.payload)
        except SourceCollectionError as exc:
            jobs.fail(job.context, job.id, self.owner, code=exc.code, detail=str(exc), retryable=False)
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            run_source_collection(
                request.context,
                connection_id=request.connection_id,
                target_date=request.target_date,
                run_generation=request.run_generation,
            )
            jobs.succeed(job.context, job.id, self.owner)
        except SourceCollectionFailed as exc:
            jobs.fail(
                job.context, job.id, self.owner,
                code="scale_collect_sources_failed", detail=str(exc), retryable=True,
            )
        except Exception as exc:
            jobs.fail(
                job.context, job.id, self.owner,
                code="scale_collect_sources_failed", detail=f"{type(exc).__name__}: {exc}", retryable=True,
            )

    @runs_as(WORKER_ROLE)
    def _scale_export(self, job: jobs.ClaimedJob) -> None:
        """Dispatch fence for the ``scale_export`` job (#55).

        This Worker is the one deployed (``python -m worker.main``), so it
        is the one that has to run Scale exports. Like ``_scale_evaluate``
        it only delegates to ``app.scale``: ``run_scale_export`` never sees
        this runtime's ``meta_client``/``launch_adapter``, and Scale makes
        no Meta write (ADR 0017 sets the conditions under which this shared
        Worker may run it). ``run_scale_export`` settles the Operation itself,
        in one transaction under the export's row lock, so a rerun after a
        crash (``crash=requeue`` in ``app.platform.job_policy``) finds the
        export completed and only closes the Job.
        """
        from app.scale.exports import ExportError, export_job_context, run_scale_export

        export_id = str(job.payload.get("export_id") or "")
        try:
            if not export_id:
                raise ExportError("missing_input", "缺少 export_id")
            context = export_job_context(job.context, job.payload)
        except ExportError as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code=exc.code,
                detail=str(exc),
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(context) as session:
                run_scale_export(session, context, export_id)
            jobs.succeed(job.context, job.id, self.owner)
        except ExportError as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code=exc.code,
                detail=str(exc),
                retryable=False,
            )
        except Exception as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="scale_export_failed",
                detail=str(exc),
                retryable=False,
            )

    @runs_as(WORKER_ROLE)
    def _meta_schedule_tick(self, job: jobs.ClaimedJob) -> None:
        schedule_id = str(job.payload.get("schedule_id") or "")
        if not schedule_id:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="missing_input",
                detail="缺少 schedule_id",
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            meta_service.run_due_schedule(job.context, schedule_id)
            jobs.succeed(job.context, job.id, self.owner)
        except Exception as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="meta_schedule_failed",
                detail=str(exc),
                retryable=True,
            )

    async def _dispatch_observation_ticks_if_due(self) -> None:
        """Turn due Meta observation schedules into tick Jobs, at most once a
        minute per process (#107). A failure is reported and retried on the
        next interval; it never stops this lane from claiming Jobs."""
        from .meta import observation_sync

        now = time.monotonic()
        if self._next_observation_dispatch_at is not None and now < self._next_observation_dispatch_at:
            return
        # Set before awaiting: the other main-lane tasks of this process see
        # it and skip, so one process dispatches once per interval.
        self._next_observation_dispatch_at = now + observation_sync.DISPATCH_INTERVAL_SECONDS
        try:
            await asyncio.to_thread(observation_sync.dispatch_due_observation_ticks)
        except Exception as exc:  # noqa: BLE001 -- the next interval retries
            from .platform.ops_alerts import AlertCategory, Severity, emit

            emit(
                Severity.WARNING,
                AlertCategory.WORKER_LIFECYCLE,
                "Meta observation dispatch failed; retrying next interval",
                details={"owner": self.owner, "error": type(exc).__name__},
            )

    @runs_as(WORKER_ROLE)
    def _meta_observation_tick(self, job: jobs.ClaimedJob) -> None:
        """One due firing of a connection's observation schedules (#107).

        The Meta mode comes from the payload: the claimed context names
        none, and RLS shows Meta rows only to a context that does. The
        schedules are re-checked under their row lock, so a replayed tick
        changes nothing.
        """
        from .meta.observation_sync import (
            ObservationSyncError,
            observation_job_context,
            parse_fire_at,
            run_observation_tick,
        )

        try:
            context = observation_job_context(job.context, job.payload)
            connection_id = str(job.payload.get("connection_id") or "")
            if not connection_id:
                raise ObservationSyncError("missing_input", "缺少 connection_id")
            fire_at = parse_fire_at(str(job.payload.get("fire_at") or ""))
        except (ObservationSyncError, ValueError) as exc:
            jobs.fail(job.context, job.id, self.owner, code="missing_input", detail=str(exc), retryable=False)
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(context) as session:
                run_observation_tick(session, context, connection_id=connection_id, fire_at=fire_at)
            jobs.succeed(job.context, job.id, self.owner)
        except Exception as exc:
            jobs.fail(
                job.context, job.id, self.owner,
                code="meta_observation_tick_failed", detail=str(exc), retryable=True,
            )

    @runs_as(WORKER_ROLE)
    def _meta_observation_sync(self, job: jobs.ClaimedJob) -> None:
        """Run one connection's sync runs for one target day to a terminal
        state (#107). Reads go through the sandbox adapter, resolved per
        transaction and never held by this runtime (ADR 0017). Runs resume
        from their last committed page; on the Job's last attempt the runs
        that still fail are marked ``failed``.

        Once every run is terminal -- all succeeded, or the last attempt
        failed the rest -- Scale's collection of the batch is enqueued
        (#109, #101 S3) before the Job is settled: a failed batch becomes a
        blocked manifest, not a silent gap. The collection key names the
        batch, so a rerun of this Job names the same collection Job."""
        from datetime import date

        from .meta.observation_sync import (
            ObservationBatchFailed,
            ObservationLeaseLost,
            ObservationSyncError,
            observation_job_context,
            run_observation_sync,
        )

        try:
            context = observation_job_context(job.context, job.payload)
            connection_id = str(job.payload.get("connection_id") or "")
            if not connection_id:
                raise ObservationSyncError("missing_input", "缺少 connection_id")
            target_date = date.fromisoformat(str(job.payload.get("target_date") or ""))
            run_generation = int(job.payload.get("run_generation") or 1)
        except (ObservationSyncError, ValueError, TypeError) as exc:
            jobs.fail(job.context, job.id, self.owner, code="missing_input", detail=str(exc), retryable=False)
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            run_observation_sync(
                context,
                connection_id=connection_id,
                target_date=target_date,
                run_generation=run_generation,
                final_attempt=job.attempt >= job.max_attempts,
                heartbeat=lambda: jobs.heartbeat(job.context, job.id, self.owner),
            )
            self._enqueue_source_collection(context, connection_id, target_date, run_generation)
            jobs.succeed(job.context, job.id, self.owner)
        except ObservationLeaseLost:
            return  # the Job is no longer ours to settle
        except ObservationBatchFailed as exc:
            detail = str(exc)
            if exc.final:
                try:
                    self._enqueue_source_collection(context, connection_id, target_date, run_generation)
                except Exception as enqueue_error:  # noqa: BLE001 -- the batch's failure is still recorded
                    detail = f"{detail}; collection not enqueued: {type(enqueue_error).__name__}"
            jobs.fail(
                job.context, job.id, self.owner,
                code="meta_observation_sync_failed", detail=detail, retryable=not exc.final,
            )
        except Exception as exc:
            jobs.fail(
                job.context, job.id, self.owner,
                code="meta_observation_sync_failed", detail=f"{type(exc).__name__}: {exc}", retryable=True,
            )

    @staticmethod
    def _enqueue_source_collection(context: TenantContext, connection_id: str, target_date, run_generation: int) -> None:
        """Scale's collection of a finished batch (#109); nothing when no
        Enrollment on the connection could use it."""
        from .scale.collector import enqueue_source_collection

        with tenant_session(context) as session:
            enqueue_source_collection(
                session, context, connection_id=connection_id, target_date=target_date, run_generation=run_generation,
            )

    @runs_as(WORKER_ROLE)
    async def _launch_canary(self, job: jobs.ClaimedJob) -> None:
        operation_id = str(job.payload.get("operation_id") or "")
        if not operation_id:
            jobs.fail(job.context, job.id, self.owner, code="missing_input", detail="缺少 operation_id", retryable=False)
            return
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            await launch_service.execute_operation(job.context, operation_id, self.launch_adapter)
            jobs.succeed(job.context, job.id, self.owner)
        except MetaWriteError as exc:
            state = jobs.fail(
                job.context, job.id, self.owner, code=exc.code, detail=str(exc), retryable=exc.retryable,
            )
            launch_service.record_failure(
                job.context, operation_id, exc.code, str(exc), terminal=state == "failed"
            )
        except Exception as exc:
            state = jobs.fail(
                job.context, job.id, self.owner, code="launch_saga_failed", detail=str(exc), retryable=False,
            )
            launch_service.record_failure(
                job.context, operation_id, "launch_saga_failed", str(exc), terminal=state == "failed"
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _launch_job_context(job: jobs.ClaimedJob) -> TenantContext:
        mode = str(job.payload.get("meta_mode") or "")
        if mode not in {"customer_sandbox", "controlled_mock", "internal_validation"}:
            raise ValueError("missing or invalid meta_mode")
        return replace(job.context, meta_mode=mode)

    @runs_as(WORKER_ROLE)
    def _launch_provisioning(self, job: jobs.ClaimedJob) -> None:
        operation_id = str(job.payload.get("operation_id") or "")
        provisioning_id = str(job.payload.get("provisioning_id") or "")
        if not operation_id or not provisioning_id:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail="缺少 operation_id 或 provisioning_id",
                retryable=False,
            )
            return
        try:
            context = self._launch_job_context(job)
        except ValueError as exc:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail=str(exc), retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(context) as session:
                operation = get_operation(session, operation_id)
                if operation.state == OperationState.QUEUED.value:
                    transition_operation(
                        session, operation_id, OperationState.RUNNING,
                        reason="launch provisioning worker started",
                        actor=context.executor,
                    )
                execute_provisioning(
                    session,
                    context,
                    provisioning_id,
                    eligibility_validator=lambda current_session, current_context, preview: (
                        launch_service.validate_multi_candidate_preview_integrity(
                            current_session, current_context, preview,
                        )
                    ),
                )
            with tenant_session(context) as session:
                operation = get_operation(session, operation_id)
                if operation.state == OperationState.RUNNING.value:
                    transition_operation(
                        session, operation_id, OperationState.SUCCEEDED,
                        reason="all PAUSED objects provisioned and verified",
                        actor=context.executor,
                        result_ref=OperationResultRef("launch_provisioning", provisioning_id),
                    )
            jobs.succeed(job.context, job.id, self.owner)
        except ProvisioningUncertain as exc:
            with tenant_session(context) as session:
                operation = get_operation(session, operation_id)
                if operation.state == OperationState.RUNNING.value:
                    transition_operation(
                        session, operation_id, OperationState.UNCERTAIN,
                        reason=str(exc)[:500], actor=context.executor,
                    )
            jobs.mark_uncertain(
                job.context, job.id, self.owner,
                code="launch_outcome_uncertain", detail=str(exc),
            )
        except ProvisioningError as exc:
            self._fail_launch_provisioning(
                job, context, operation_id, provisioning_id, str(exc),
            )
        except Exception as exc:  # noqa: BLE001 -- one Launch must not stop the Worker
            # A database error inside the saga aborts its transaction, so the
            # saga's own ``-> failed`` write raises as well. Record the outcome
            # in a fresh unit of work and keep serving every other Job.
            self._fail_launch_provisioning(
                job, context, operation_id, provisioning_id,
                f"{type(exc).__name__}: {exc}",
            )

    def _fail_launch_provisioning(
        self,
        job: jobs.ClaimedJob,
        context: TenantContext,
        operation_id: str,
        provisioning_id: str,
        detail: str,
    ) -> None:
        try:
            with tenant_session(context) as session:
                record_provisioning_failure(
                    session, context, provisioning_id, reason=detail,
                )
                operation = get_operation(session, operation_id)
                if operation.state == OperationState.QUEUED.value:
                    # The QUEUED -> RUNNING transition was rolled back with
                    # the saga's unit of work; replay it so the failure is a
                    # legal, audited transition rather than a stuck command.
                    operation = transition_operation(
                        session, operation_id, OperationState.RUNNING,
                        reason="launch provisioning worker started",
                        actor=context.executor,
                    )
                if operation.state == OperationState.RUNNING.value:
                    transition_operation(
                        session, operation_id, OperationState.FAILED,
                        reason=detail[:500], actor=context.executor,
                    )
        finally:
            jobs.fail(
                job.context, job.id, self.owner,
                code="launch_provisioning_failed", detail=detail, retryable=False,
            )

    @runs_as(WORKER_ROLE)
    def _launch_recovery(self, job: jobs.ClaimedJob) -> None:
        reconciliation_id = str(job.payload.get("reconciliation_id") or "")
        if not reconciliation_id:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail="缺少 reconciliation_id", retryable=False,
            )
            return
        try:
            context = self._launch_job_context(job)
        except ValueError as exc:
            jobs.fail(
                job.context, job.id, self.owner,
                code="missing_input", detail=str(exc), retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(context) as session:
                execute_recovery(session, context, reconciliation_id)
            jobs.succeed(job.context, job.id, self.owner)
        except RecoveryError as exc:
            jobs.fail(
                job.context, job.id, self.owner,
                code="launch_recovery_failed", detail=str(exc), retryable=False,
            )

    @runs_as(WORKER_ROLE)
    async def _meta_read_ingestion(self, job: jobs.ClaimedJob) -> None:
        ingestion_id = str(job.payload.get("ingestion_id") or "")
        if not ingestion_id:
            jobs.fail(job.context, job.id, self.owner, code="missing_input", detail="缺少 ingestion_id", retryable=False)
            return
        jobs.mark_running(job.context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(job.context, job.id))
        try:
            await meta_service.process_ingestion(job.context, ingestion_id, self.meta_client)
            jobs.succeed(job.context, job.id, self.owner)
        except MetaApiError as exc:
            state = jobs.fail(
                job.context,
                job.id,
                self.owner,
                code=exc.code,
                detail=str(exc),
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after,
            )
            meta_service.record_ingestion_failure(
                job.context, ingestion_id, exc.code, str(exc), terminal=state == "failed"
            )
        except meta_service.MetaIngestionNotReady as exc:
            state = jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="meta_prerequisite_pending",
                detail=str(exc),
                retryable=True,
                retry_after_seconds=1,
            )
            meta_service.record_ingestion_failure(
                job.context, ingestion_id, "meta_prerequisite_pending", str(exc), terminal=state == "failed"
            )
        except Exception as exc:
            state = jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="meta_projection_failed",
                detail=str(exc),
                retryable=False,
            )
            meta_service.record_ingestion_failure(
                job.context, ingestion_id, "meta_projection_failed", str(exc), terminal=state == "failed"
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    @runs_as(WORKER_ROLE)
    async def _generate_asset(self, job: jobs.ClaimedJob) -> None:
        # ---- dispatch fence ----
        # Jobs with execution_contract_version >= 1 go through the
        # ExternalCallCoordinator (new entitlement contract path); legacy
        # jobs (version 0) use the existing generate_with_retry path.
        # The version is carried in the job payload by the admission module;
        # it also lives on the JobRecord column, but ClaimedJob only
        # exposes the payload dict.
        ecv = int(job.payload.get("execution_contract_version", 0) or 0)
        if ecv >= 1:
            await self._generate_asset_coordinated(job)
            return

        await self._generate_asset_legacy(job)

    async def _generate_asset_coordinated(self, job: jobs.ClaimedJob) -> None:
        """New-contract path: delegate to ExternalCallCoordinator.

        The coordinator handles credential resolution, attempt tracking,
        and settlement.  The Worker only needs to translate the outcome
        into the asset lifecycle.
        """
        import hashlib

        from app.entitlements.coordinator import (
            CoordinatorError,
            ExternalCallCoordinator,
        )

        asset_id = str(job.payload["asset_id"])
        context = job.context

        # Build a deterministic semantic key for this call
        call_semantic_key_hash = hashlib.sha256(
            f"generate_asset:{context.workspace_id}:{job.id}:{asset_id}".encode()
        ).hexdigest()

        if self._coordinator is None:
            store.fail_asset(
                context, asset_id,
                code="coordinator_not_configured",
                detail="ExternalCallCoordinator is not available",
                job_id=job.id,
            )
            jobs.fail(
                context, job.id, self.owner,
                code="coordinator_not_configured",
                detail="ExternalCallCoordinator is not available",
                retryable=False,
            )
            return

        store.mark_asset_running(context, asset_id)

        try:
            outcome = await self._coordinator.execute(
                context,
                job_id=job.id,
                call_semantic_key_hash=call_semantic_key_hash,
            )
        except CoordinatorError as exc:
            store.fail_asset(
                context, asset_id,
                code="coordinator_failed", detail=str(exc), job_id=job.id,
            )
            jobs.fail(
                context, job.id, self.owner,
                code="coordinator_failed", detail=str(exc), retryable=False,
            )
            return

        if outcome.success:
            # The coordinator already settled the reservation and recorded
            # the job as completed; update the asset record.
            store.complete_asset(
                context, asset_id,
                path=None,  # coordinated path does not write local files
                job_id=job.id,
            )
        else:
            store.fail_asset(
                context, asset_id,
                code=outcome.error_code or "dispatch_failed",
                detail=outcome.error_detail or "external call failed",
                job_id=job.id,
            )
            if outcome.dispatch_certainty != "dispatched":
                jobs.fail(
                    context, job.id, self.owner,
                    code=outcome.error_code or "dispatch_failed",
                    detail=outcome.error_detail or "external call failed",
                    retryable=False,
                )

    async def _generate_asset_legacy(self, job: jobs.ClaimedJob) -> None:
        """Legacy path: direct provider call with generate_with_retry."""
        product_id = str(job.payload["product_id"])
        run_id = str(job.payload["run_id"])
        asset_id = str(job.payload["asset_id"])
        context = job.context
        product = store.get_product(context, product_id)
        run = store.get_run(context, product_id, run_id)
        asset_row = store.get_asset(context, asset_id)
        if not product or not run or not asset_row:
            jobs.fail(
                context,
                job.id,
                self.owner,
                code="missing_input",
                detail="产品、Run 或素材记录不存在",
                retryable=False,
            )
            return
        if asset_row.status == "ok":
            jobs.mark_running(context, job.id, self.owner)
            jobs.succeed(context, job.id, self.owner)
            return
        if run.status == "cancelled":
            # The interface words `cancelled` by code; `detail` is a log diagnostic (#286).
            store.fail_asset(context, asset_id, code="cancelled", detail="run cancelled before generation", job_id=job.id)
            jobs.fail(context, job.id, self.owner, code="cancelled", detail="run cancelled before generation", retryable=False)
            return

        provider = get_provider(asset_row.provider)
        ref_paths = [store.refs_dir(product_id) / name for name in product.ref_images] if run.use_ref else []
        out_path = store.run_dir(product_id, run_id) / asset_row.filename
        temp_path = out_path.with_suffix(out_path.suffix + f".{job.id}.tmp")
        store.mark_asset_running(context, asset_id)

        # Crossing this transition means a paid non-idempotent request may now
        # exist remotely.  Crash recovery must not auto-retry it blindly.
        jobs.mark_running(context, job.id, self.owner)
        heartbeat = asyncio.create_task(self._heartbeat(context, job.id))
        try:
            image = await provider.generate_with_retry(
                asset_row.prompt,
                ref_paths,
                run.aspect_ratio,
                run.image_size,
                run.model,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("wb") as handle:
                handle.write(image)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(out_path)
            store.complete_asset(context, asset_id, out_path, job_id=job.id)
            jobs.succeed(context, job.id, self.owner)
        except ProviderError as exc:
            store.fail_asset(context, asset_id, code="provider_failed", detail=str(exc), job_id=job.id)
            jobs.fail(
                context,
                job.id,
                self.owner,
                code="provider_failed",
                detail=str(exc),
                retryable=False,
            )
        except Exception as exc:
            # Provider wrappers already perform safe network retries.  At this
            # point the outcome may be ambiguous, so do not submit again here.
            store.fail_asset(context, asset_id, code="worker_failed", detail=str(exc), job_id=job.id)
            jobs.fail(
                context,
                job.id,
                self.owner,
                code="worker_failed",
                detail=str(exc),
                retryable=False,
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            temp_path.unlink(missing_ok=True)

    async def _heartbeat(self, context: TenantContext, job_id: str) -> None:
        # Short leases are used by crash/competition tests and are also valid
        # operationally.  Sleeping for at least one second can therefore let
        # a one-second lease expire before its first renewal.  Keep a small
        # scheduling floor while always renewing comfortably inside the
        # configured lease window.
        interval = max(0.1, config.JOB_LEASE_SECONDS / 3)
        while True:
            await asyncio.sleep(interval)
            if not jobs.heartbeat(context, job_id, self.owner):
                return

    async def close(self) -> None:
        if self._onboarding_http_client is not None:
            await self._onboarding_http_client.aclose()


def _warn_if_onboarding_delivery_unconfigured(base_url: str, service_token: str) -> None:
    """Name an empty onboarding-delivery setting at startup, as booleans only.

    Without both settings `WorkerRuntime` builds no delivery client and skips
    onboarding facts without an error; this line shows the gap in the deploy
    log instead. Values never reach the log.
    """
    if base_url and service_token:
        return
    logger.warning(
        "onboarding_delivery_config base_url_set=%s token_set=%s",
        str(bool(base_url)).lower(),
        str(bool(service_token)).lower(),
    )


# ADR 0017: this Worker runs Scale handlers (`scale_evaluate`,
# `scale_advice_expire`, `scale_collect_sources`, `scale_export`), so the process must hold no Meta
# write capability. Every concrete Meta write
# client in the codebase is listed here; each refuses construction without the
# write configuration (`config.meta_write_ready()`, which needs a non-empty
# `META_WRITE_ACCESS_TOKEN`). `tests/test_shared_worker_meta_write_tripwire.py`
# fails when a write client appears that this tuple does not list.
_META_WRITE_CLIENTS: tuple[type, ...] = (HttpMetaWriteAdapter,)
# An object whose class has one of these methods is a Meta write port:
# `MetaWritePort.ensure_step` (Launch canary) or `MetaAdapterPort.ensure_object`
# (the Launch provisioning saga's adapter).
_META_WRITE_PORT_METHODS = ("ensure_step", "ensure_object")


class MetaWriteCapabilityError(RuntimeError):
    """The Worker that runs Scale handlers could write to Meta (ADR 0017)."""


def meta_write_capability_findings(runtime: WorkerRuntime) -> list[str]:
    """Name every way this Worker process could write to Meta (ADR 0017).

    Three conditions, each checked against the running process rather than
    the deployment contract: no write Token in the environment or config, no
    write port held by the composed runtime, and no write client that can be
    constructed under this process's configuration. That last check is also
    the Launch dispatch condition: `launch_canary` builds the write client
    itself when no port is injected, so it reaches Meta only if
    `HttpMetaWriteAdapter()` succeeds. The provisioning and recovery sagas use
    the sandbox adapter only. Findings name the condition, never a value.
    """
    findings: list[str] = []
    if os.environ.get("META_WRITE_ACCESS_TOKEN", "").strip() or config.META_WRITE_ACCESS_TOKEN:
        findings.append("meta_write_access_token_set")
    for name, value in sorted(vars(runtime).items()):
        if value is not None and any(
            inspect.getattr_static(type(value), method, None) is not None
            for method in _META_WRITE_PORT_METHODS
        ):
            findings.append(f"meta_write_port_held:{name}")
    for client in _META_WRITE_CLIENTS:
        try:
            client()
        except MetaWriteError:
            continue
        findings.append(f"meta_write_client_constructible:{client.__name__}")
    return findings


def assert_no_meta_write_capability(runtime: WorkerRuntime) -> None:
    """Refuse to run Scale handlers in a Worker that could write to Meta.

    ADR 0017 lets Scale share this Worker only while the process has no Meta
    write capability; once it has one, the Scale Worker must be split out
    first. Failing at startup keeps a misconfigured process from claiming any
    Job.
    """
    findings = meta_write_capability_findings(runtime)
    if findings:
        raise MetaWriteCapabilityError(
            "this Worker runs Scale handlers and must have no Meta write capability "
            "(ADR 0017); split out the Scale Worker before enabling Meta writes: "
            + ", ".join(findings)
        )


async def _main(once: bool) -> None:
    assert_runtime_role("marketai_domain_worker")
    # Unconditional: production Worker contracts deny `DELEGATED_JWKS_URL`, so
    # `DELEGATED_IDENTITY_ENABLED` is always off here.
    _warn_if_onboarding_delivery_unconfigured(
        config.CONTROL_PLANE_INTERNAL_BASE_URL, config.DOMAIN_TO_CONTROL_PLANE_SYNC_TOKEN
    )
    runtime = WorkerRuntime()
    try:
        assert_no_meta_write_capability(runtime)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, runtime._begin_drain)
            except NotImplementedError:  # pragma: no cover - Windows
                pass
        if once:
            await runtime.run_once()
        else:
            await runtime.run_forever()
    finally:
        await runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MarketAI persistent worker")
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
    args = parser.parse_args()
    asyncio.run(_main(args.once))


if __name__ == "__main__":
    main()
