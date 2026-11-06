"""Regression coverage for Scale partial indexes tracked by Alembic.

The Scale migration creates these indexes with PostgreSQL-specific DDL.  They
must also be present in SQLAlchemy metadata or ``alembic check`` interprets
them as database-only objects and proposes removing them on every release.
"""
from __future__ import annotations

import unittest

from app.adapters.persistence.database import Base
from app.scale import models as _scale_models  # noqa: F401  (register tables)


_EXPECTED_PARTIAL_INDEXES = {
    "ix_senr_active_binding": (
        "scale_enrollments",
        (
            "workspace_id",
            "meta_mode",
            "meta_connection_id",
            "ad_account_id",
            "remote_binding_key",
        ),
        "lifecycle IN ('active', 'paused')",
    ),
    "ix_sat_active_thread": (
        "scale_advice_threads",
        (
            "workspace_id",
            "meta_mode",
            "episode_id",
            "advice_type",
            "reason_signature",
        ),
        "lifecycle = 'active'",
    ),
    "ix_stgt_active_target": (
        "scale_target_versions",
        (
            "workspace_id",
            "product_version_id",
            "offer_version_id",
            "market_code",
            "currency",
        ),
        "lifecycle_state = 'active'",
    ),
    "ix_sad_thread_idempotency": (
        "scale_advice_dispositions",
        ("workspace_id", "meta_mode", "thread_id", "idempotency_key"),
        "idempotency_key IS NOT NULL",
    ),
    "ix_sas_thread_idempotency": (
        "scale_advice_snoozes",
        ("workspace_id", "meta_mode", "thread_id", "idempotency_key"),
        "idempotency_key IS NOT NULL",
    ),
}


class ScaleModelPartialIndexTests(unittest.TestCase):
    def test_migration_owned_partial_indexes_are_in_metadata(self) -> None:
        for index_name, (
            table_name,
            column_names,
            predicate,
        ) in _EXPECTED_PARTIAL_INDEXES.items():
            table = Base.metadata.tables[table_name]
            matching = [index for index in table.indexes if index.name == index_name]
            self.assertEqual(1, len(matching), index_name)

            index = matching[0]
            self.assertTrue(index.unique, index_name)
            self.assertEqual(
                column_names,
                tuple(column.name for column in index.columns),
                index_name,
            )
            self.assertEqual(
                predicate,
                str(index.dialect_options["postgresql"]["where"]),
                index_name,
            )
            self.assertIsNotNone(index._ddl_if, index_name)
            self.assertEqual("postgresql", index._ddl_if.dialect, index_name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
