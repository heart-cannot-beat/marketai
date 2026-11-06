"""Durable application-owned staging cleanup behavior."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.platform.blob_store import BlobStoreError
from app.platform.jobs import ClaimedJob
from app.platform.tenancy import system_context
from app.worker_runtime import WorkerRuntime


class _DeleteStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.keys: list[str] = []
        self.error = error

    async def delete(self, ref) -> None:
        self.keys.append(ref.key)
        if self.error is not None:
            raise self.error


def _job(not_before: datetime) -> ClaimedJob:
    return ClaimedJob(
        id="job_cleanup",
        type="cleanup_staging_upload",
        payload={
            "intent_id": "ui_1",
            "staging_key": "staging/ws1/generation_output/2026/09/22/a.dat",
            "not_before": not_before.isoformat(),
        },
        attempt=1,
        max_attempts=10,
        lease_owner="worker-1",
        context=system_context("ws1", initiator_id="actor-1"),
    )


class StagingCleanupTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, store: _DeleteStore) -> WorkerRuntime:
        runtime = object.__new__(WorkerRuntime)
        runtime.owner = "worker-1"
        runtime.blob_store = store
        return runtime

    async def test_cleanup_waits_until_presigned_upload_capability_expires(
        self,
    ) -> None:
        store = _DeleteStore()
        runtime = self._runtime(store)
        with (
            patch("app.worker_runtime.jobs.mark_running") as mark_running,
            patch("app.worker_runtime.jobs.fail") as fail,
            patch("app.worker_runtime.jobs.succeed") as succeed,
        ):
            await runtime._cleanup_staging_upload(  # noqa: SLF001
                _job(datetime.now(timezone.utc) + timedelta(minutes=5))
            )
        mark_running.assert_called_once()
        fail.assert_called_once()
        self.assertTrue(fail.call_args.kwargs["retryable"])
        self.assertGreater(fail.call_args.kwargs["retry_after_seconds"], 0)
        succeed.assert_not_called()
        self.assertEqual(store.keys, [])

    async def test_cleanup_delete_is_replayable_after_expiry(self) -> None:
        store = _DeleteStore()
        runtime = self._runtime(store)
        job = _job(datetime.now(timezone.utc) - timedelta(seconds=1))
        with (
            patch("app.worker_runtime.jobs.mark_running"),
            patch("app.worker_runtime.jobs.fail") as fail,
            patch("app.worker_runtime.jobs.succeed") as succeed,
        ):
            await runtime._cleanup_staging_upload(job)  # noqa: SLF001
            await runtime._cleanup_staging_upload(job)  # noqa: SLF001
        fail.assert_not_called()
        self.assertEqual(succeed.call_count, 2)
        self.assertEqual(store.keys, [job.payload["staging_key"]] * 2)

    async def test_cleanup_persists_only_redacted_storage_error(self) -> None:
        store = _DeleteStore(
            BlobStoreError("Railway Bucket delete failed (AccessDenied)")
        )
        runtime = self._runtime(store)
        with (
            patch("app.worker_runtime.jobs.mark_running"),
            patch("app.worker_runtime.jobs.fail") as fail,
            patch("app.worker_runtime.jobs.succeed") as succeed,
        ):
            await runtime._cleanup_staging_upload(  # noqa: SLF001
                _job(datetime.now(timezone.utc) - timedelta(seconds=1))
            )
        fail.assert_called_once()
        self.assertEqual(
            fail.call_args.kwargs["detail"],
            "Railway Bucket delete failed (AccessDenied)",
        )
        self.assertTrue(fail.call_args.kwargs["retryable"])
        succeed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
