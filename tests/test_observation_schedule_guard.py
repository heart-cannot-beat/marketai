"""The guard against Meta observation schedules left for a later test's Worker (#124).

``tests/conftest.py`` asks :class:`ObservationScheduleGuard` after every test
which connections now have a schedule the Worker's observation dispatch would
turn into tick Jobs, and fails the teardown of the test that left one. These
tests ask a guard of their own, about their own Workspace only, and pause the
schedule in cleanup (``_quiesce``), so the suite's guard finds nothing here.
"""
from __future__ import annotations

from app.adapters.persistence import database
from tests.support.queue_hygiene import ObservationScheduleGuard, observation_schedule_failure
from tests.test_meta_observation_sync import ObservationSyncTestCase


class ObservationScheduleGuardTests(ObservationSyncTestCase):
    def _left(self, guard: ObservationScheduleGuard) -> list:
        return [(slot, due) for slot, due in guard.unreported() if slot.workspace_id == self.workspace_id]

    def _guard(self) -> ObservationScheduleGuard:
        return ObservationScheduleGuard(database.engine.url.render_as_string(hide_password=False))

    def test_a_schedule_left_active_is_reported_once_and_names_the_test(self):
        connection, generation = self._sandbox()
        guard = self._guard()

        [(slot, due)] = self._left(guard)
        self.assertEqual((slot.connection_id, slot.meta_mode, due), (connection["id"], "customer_sandbox", False))
        message = observation_schedule_failure([(slot, due)], "tests/test_example.py::Example::test_leaves_it")
        self.assertIn("tests/test_example.py::Example::test_leaves_it left Meta observation schedules", message)
        self.assertIn(f"Workspace {self.workspace_id}, customer_sandbox connection {connection['id']}: due at ",
                      message)
        self.assertEqual(self._left(guard), [], "each connection is reported once, to the test that left it")

        fire_at = self._make_due(generation["id"])
        [(slot, due)] = self._left(self._guard())
        self.assertEqual((slot.fire_at, due), (fire_at, True))
        self.assertIn(": due now", observation_schedule_failure([(slot, due)], "t"))

    def test_a_paused_schedule_is_not_reported(self):
        _connection, generation = self._sandbox()
        self._make_due(generation["id"])
        self._quiesce(self.workspace_id)
        self.assertEqual(self._left(self._guard()), [])

    def test_a_test_that_swapped_the_engine_is_not_checked(self):
        self._sandbox()
        guard = ObservationScheduleGuard("sqlite:////nonexistent/other.db")
        self.assertEqual(guard.unreported(), [])
