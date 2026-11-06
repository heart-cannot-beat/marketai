"""Tests for retention enforcement (spec §110).

Covers:
  - Protected tables (audit_events, cost_ledger, etc.) cannot be purged.
  - Retention categories target only non-protected tables.
  - enforce_retention raises RetentionProtectionError for protected tables.
  - validate_no_protected_deletion catches misconfigurations.
"""
from __future__ import annotations

import unittest

from app.platform.retention import (
    PROTECTED_TABLES,
    RetentionCategory,
    RetentionProtectionError,
    default_categories,
    validate_no_protected_deletion,
)


class TestProtectedTables(unittest.TestCase):
    def test_audit_events_is_protected(self):
        self.assertIn("audit_events", PROTECTED_TABLES)

    def test_cost_ledger_is_protected(self):
        self.assertIn("cost_ledger", PROTECTED_TABLES)

    def test_launch_audit_is_protected(self):
        self.assertIn("launch_audit_log", PROTECTED_TABLES)

    def test_verdict_decisions_is_protected(self):
        self.assertIn("verdict_decisions", PROTECTED_TABLES)

    def test_library_audit_is_protected(self):
        self.assertIn("library_audit", PROTECTED_TABLES)


class TestDefaultCategories(unittest.TestCase):
    def test_no_default_category_targets_protected_table(self):
        for cat in default_categories():
            self.assertNotIn(
                cat.table_name,
                PROTECTED_TABLES,
                f"Default category {cat.name!r} targets protected table "
                f"{cat.table_name!r}",
            )


class TestValidateNoProtectedDeletion(unittest.TestCase):
    def test_safe_categories_pass(self):
        cats = [
            RetentionCategory("logs", "worker_logs", 30),
            RetentionCategory("traces", "traces", 7),
        ]
        validate_no_protected_deletion(cats)

    def test_protected_table_raises(self):
        cats = [
            RetentionCategory("audit", "audit_events", 90),
        ]
        with self.assertRaises(RetentionProtectionError) as cm:
            validate_no_protected_deletion(cats)
        self.assertEqual(cm.exception.table_name, "audit_events")

    def test_cost_ledger_raises(self):
        cats = [RetentionCategory("costs", "cost_ledger", 365)]
        with self.assertRaises(RetentionProtectionError):
            validate_no_protected_deletion(cats)


class TestEnforceRetentionProtection(unittest.TestCase):
    def test_enforce_retention_rejects_protected_table(self):
        from unittest.mock import MagicMock

        from app.platform.retention import enforce_retention

        session = MagicMock()
        bad_cats = [RetentionCategory("audit", "audit_events", 90)]
        with self.assertRaises(RetentionProtectionError):
            enforce_retention(session, bad_cats)
        session.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
