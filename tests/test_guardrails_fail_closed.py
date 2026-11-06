"""Tests that spend guardrails fail closed (spec §110).

Covers:
  - Missing guardrail row blocks the operation (NoResultFound propagates).
  - A database error during guardrail check blocks the operation.
  - GuardrailExceededError blocks the operation with the limit name.
"""
from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from sqlalchemy.orm.exc import NoResultFound

from app.entitlements.reservation_book import (
    GuardrailExceededError,
    ReservationBook,
)


class TestGuardrailsFailClosed(unittest.TestCase):
    def _make_mock_session(self, *, guardrail_exists: bool = True, raise_on_query: Exception | None = None):
        session = MagicMock()
        query = MagicMock()
        session.query.return_value = query
        filter_by = MagicMock()
        query.filter_by.return_value = filter_by
        filter_by.with_for_update.return_value = filter_by

        if raise_on_query is not None:
            filter_by.one.side_effect = raise_on_query
        elif not guardrail_exists:
            filter_by.one.side_effect = NoResultFound("no guardrail")
        else:
            guardrail = MagicMock()
            guardrail.workspace_id = "ws_test"
            guardrail.per_operation_limit = Decimal("5")
            guardrail.rolling_24h_limit = Decimal("20")
            guardrail.period_limit = Decimal("100")
            guardrail.outstanding_exposure_limit = Decimal("10")
            guardrail.revision = 1
            guardrail.platform_policy_version = 1
            filter_by.one.return_value = guardrail

        bind = MagicMock()
        bind.dialect.name = "sqlite"
        session.get_bind.return_value = bind

        return session

    def _make_mock_context(self):
        context = MagicMock()
        context.workspace_id = "ws_test"
        context.initiator_id = "user_test"
        return context

    def _make_mock_plan(self, amount: Decimal = Decimal("1.00")):
        plan = MagicMock()
        plan.quote_bundle.total_budget_amount = amount
        plan.quote_bundle.call_quotes = []
        plan.operation_id = "op_test"
        plan.job_ids_by_call_key = {}
        plan.entitlement_ref.period_start = None
        plan.entitlement_ref.period_end = None
        plan.entitlement_ref.entitlement_version = 1
        plan.entitlement_ref.lease_sequence = 1
        return plan

    def test_missing_guardrail_blocks_operation(self):
        """No guardrail row => NoResultFound => operation blocked."""
        session = self._make_mock_session(guardrail_exists=False)
        book = ReservationBook()

        with self.assertRaises(NoResultFound):
            book.reserve(session, self._make_mock_context(), self._make_mock_plan())

    def test_database_error_blocks_operation(self):
        """A database error during guardrail lookup blocks the operation."""
        session = self._make_mock_session(
            raise_on_query=RuntimeError("connection lost"),
        )
        book = ReservationBook()

        with self.assertRaises(RuntimeError):
            book.reserve(session, self._make_mock_context(), self._make_mock_plan())

    def test_exceeded_per_operation_limit_blocks(self):
        """Proposing more than per_operation_limit raises GuardrailExceededError."""
        session = self._make_mock_session(guardrail_exists=True)
        book = ReservationBook()

        with self.assertRaises(GuardrailExceededError) as cm:
            book.reserve(
                session,
                self._make_mock_context(),
                self._make_mock_plan(amount=Decimal("999")),
            )
        self.assertEqual(cm.exception.limit_name, "per_operation_limit")

    def test_guardrail_error_never_allows_through(self):
        """Verify the fail-closed contract: any exception in the guardrail
        path must propagate, never be swallowed."""
        session = self._make_mock_session(guardrail_exists=True)
        book = ReservationBook()

        with patch.object(book, "_check_guardrails", side_effect=ValueError("corrupt state")):
            with self.assertRaises(ValueError):
                book.reserve(
                    session,
                    self._make_mock_context(),
                    self._make_mock_plan(),
                )


if __name__ == "__main__":
    unittest.main()
