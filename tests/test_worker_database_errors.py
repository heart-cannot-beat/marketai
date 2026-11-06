"""A database error fails one Worker tick, not the Worker process.

Golden Path PR Validation run 36517751730: while a release converged the
domain roles, `jobs.reconcile_admitted_operations` was refused
`operation_records`, the error escaped `run_forever`, and the Worker exited
for good. A transient error must cost one tick; a persistent one must still
end the process so the platform restarts it.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import ProgrammingError

from app import worker_runtime
from app.worker_runtime import WorkerRuntime


def _refused() -> ProgrammingError:
    return ProgrammingError(
        "SELECT operation_records.id FROM operation_records",
        {},
        Exception("permission denied for table operation_records"),
    )


class WorkerDatabaseErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_database_error_costs_one_tick(self):
        runtime = WorkerRuntime(owner="test-db-error")
        ticks = 0

        async def run_once(**_selectors):
            nonlocal ticks
            ticks += 1
            if ticks == 1:
                raise _refused()
            runtime._begin_drain()
            return False

        with patch.object(runtime, "run_once", side_effect=run_once), patch(
            "app.worker_runtime.asyncio.sleep", new=AsyncMock()
        ) as sleep, patch("app.platform.ops_alerts.emit") as emit:
            await runtime._run_lane(None, ())

        self.assertEqual(ticks, 2)
        sleep.assert_awaited()
        warnings = [
            call for call in emit.call_args_list if call.args[1].value == "worker_lifecycle"
        ]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].args[0].value, "warning")
        self.assertEqual(warnings[0].kwargs["details"]["consecutive_errors"], 1)

    async def test_persistent_database_errors_still_end_the_worker(self):
        runtime = WorkerRuntime(owner="test-db-persistent")
        ticks = 0

        async def run_once(**_selectors):
            nonlocal ticks
            ticks += 1
            raise _refused()

        with patch.object(runtime, "run_once", side_effect=run_once), patch(
            "app.worker_runtime.asyncio.sleep", new=AsyncMock()
        ), patch("app.platform.ops_alerts.emit"):
            with self.assertRaises(ProgrammingError):
                await runtime._run_lane(None, ())

        self.assertEqual(ticks, worker_runtime._MAX_CONSECUTIVE_DB_ERRORS)

    async def test_a_successful_tick_resets_the_error_count(self):
        runtime = WorkerRuntime(owner="test-db-reset")
        outcomes = iter(["error", "ok"] * worker_runtime._MAX_CONSECUTIVE_DB_ERRORS)

        async def run_once(**_selectors):
            outcome = next(outcomes, None)
            if outcome is None:
                runtime._begin_drain()
                return False
            if outcome == "error":
                raise _refused()
            return True

        with patch.object(runtime, "run_once", side_effect=run_once), patch(
            "app.worker_runtime.asyncio.sleep", new=AsyncMock()
        ), patch("app.platform.ops_alerts.emit"):
            await runtime._run_lane(None, ())

        self.assertTrue(runtime.stopping)

    async def test_other_errors_are_not_swallowed(self):
        runtime = WorkerRuntime(owner="test-db-other")

        with patch.object(
            runtime, "run_once", side_effect=RuntimeError("handler bug")
        ), patch("app.platform.ops_alerts.emit"):
            with self.assertRaises(RuntimeError):
                await runtime._run_lane(None, ())


if __name__ == "__main__":
    unittest.main()
