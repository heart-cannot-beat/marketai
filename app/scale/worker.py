"""Capability-isolated Scale Worker runtime (Issue #25 Task 14).

Spec: docs/product/scale-advice-semantics-contract.md, ADR 0008
Task: .superpowers/sdd/2026-09-08-scale-deterministic-evidence-advice/task-14-brief.md

``ScaleWorkerRuntime`` is a dedicated composition root, deliberately separate
from :mod:`app.worker_runtime`'s ``WorkerRuntime``. It is constructed with no
Meta write token, no ``MetaWritePort``/write adapter, and no reference to
``app.launch``'s runtime -- Scale evaluation (``app.scale.service``) and
advice lifecycle (``app.scale.advice``) are both fully DB-driven (they read
already-ingested evidence via SQLAlchemy sessions; they never call an
external provider), so this Worker needs nothing beyond a database session
and the durable Job queue to do its job.

This module, and every other module under ``app/scale``, must never import
``MetaWritePort``, ``MetaWriteError``, ``meta_write_client``, or
``app.launch`` in any form -- ``tests/test_scale_worker_isolation.py``
enforces this with AST inspection (Gates 23-24) and a transitive
import-closure check (Gate 25), rather than trusting a docstring promise.
Putting a Scale job handler inside the shared ``WorkerRuntime`` (which *does*
hold a ``MetaWritePort`` for Launch jobs) would not satisfy that contract --
the isolation has to be a separate process/composition root, not a shared
one with an unreachable code path.

Scale never performs Meta writes: this Worker dispatches only
``scale_evaluate`` (spec §3.1/§3.4, ``app.scale.service.run_scale_evaluation``),
``scale_advice_expire`` (spec §3.5, ``app.scale.advice.expire_advice_thread``),
and ``scale_export`` (Issue #87 Task 5, ``app.scale.exports.run_scale_export``)
jobs. None of these calls reach ``app.adapters.meta.write_client`` or
``app.launch`` at any depth.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import uuid

from app.adapters.persistence.database import SessionLocal, assert_runtime_role, tenant_session
from app.platform import jobs
from app.platform.runtime_role import WORKER_ROLE, runs_as
from app.platform.tenancy import TenantContext
from app.scale.advice import AdviceLifecycleError, expire_advice_thread
from app.scale.exports import ExportError, export_job_context, run_scale_export
from app.scale.service import ScaleOrchestrationError, run_scale_evaluation

_JOB_POLL_SECONDS = float(os.getenv("JOB_POLL_SECONDS", "0.5"))


class ScaleWorkerRuntime:
    """Dedicated composition root for Scale jobs.

    Deliberately takes no constructor arguments beyond an optional lease
    ``owner`` name: there is no ``meta_client``, no ``launch_adapter``, no
    write token, and no coordinator to inject. Any future Scale capability
    that needs a real adapter must receive an explicit *read-only* evidence
    adapter here -- never a write-capable one.
    """

    def __init__(self, owner: str | None = None) -> None:
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stopping = False

    async def run_forever(self) -> None:
        while not self.stopping:
            handled = self.run_once()
            if not handled:
                await asyncio.sleep(_JOB_POLL_SECONDS)

    @runs_as(WORKER_ROLE)
    def run_once(self) -> bool:
        job = jobs.claim_next(self.owner)
        if job is None:
            return False
        if job.type == "scale_evaluate":
            self._scale_evaluate(job)
            return True
        if job.type == "scale_advice_expire":
            self._scale_advice_expire(job)
            return True
        if job.type == "scale_export":
            self._scale_export(job)
            return True
        jobs.fail(
            job.context,
            job.id,
            self.owner,
            code="unsupported_job_type",
            detail=f"Scale Worker 不处理任务类型: {job.type}",
            retryable=False,
        )
        return True

    @runs_as(WORKER_ROLE)
    def _scale_evaluate(self, job: jobs.ClaimedJob) -> None:
        """Dispatch fence for the ``scale_evaluate`` job (spec §3.8).

        ``run_scale_evaluation`` owns its own Phase A/B/C transaction
        boundaries internally; this method only owns the Job lifecycle
        around it, mirroring ``WorkerRuntime._generation_run_item``'s
        division of responsibility for its own domain call.
        """
        evaluation_run_id = str(job.payload.get("evaluation_run_id") or "")
        if not evaluation_run_id:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="missing_input",
                detail="缺少 evaluation_run_id",
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(job.context) as session:
                run_scale_evaluation(session, job.context, evaluation_run_id)
            jobs.succeed(job.context, job.id, self.owner)
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
        """Dispatch fence for the ``scale_advice_expire`` deadline job (spec §3.5)."""
        thread_id = str(job.payload.get("thread_id") or "")
        if not thread_id:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="missing_input",
                detail="缺少 thread_id",
                retryable=False,
            )
            return
        jobs.mark_running(job.context, job.id, self.owner)
        try:
            with tenant_session(job.context) as session:
                expire_advice_thread(session, job.context, thread_id)
            jobs.succeed(job.context, job.id, self.owner)
        except AdviceLifecycleError as exc:
            jobs.fail(
                job.context,
                job.id,
                self.owner,
                code="scale_advice_expire_failed",
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
    def _scale_export(self, job: jobs.ClaimedJob) -> None:
        """Dispatch fence for the ``scale_export`` job (Issue #87 Task 5).

        ``run_scale_export`` owns its own Operation transitions (running ->
        succeeded/failed) internally; this method only owns the Job
        lifecycle around it, matching ``_scale_evaluate``'s split.
        """
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


async def _main(once: bool) -> None:
    assert_runtime_role("marketai_domain_worker")
    runtime = ScaleWorkerRuntime()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, setattr, runtime, "stopping", True)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    if once:
        runtime.run_once()
    else:
        await runtime.run_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="MarketAI Scale Worker (capability-isolated)")
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit")
    args = parser.parse_args()
    asyncio.run(_main(args.once))


if __name__ == "__main__":
    main()
