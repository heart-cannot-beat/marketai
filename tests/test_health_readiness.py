"""Health readiness endpoint extensions — job queue and operation stats."""
from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, text


OWNER_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")
API_URL = os.environ.get("DOMAIN_API_DATABASE_URL")

# All NOT NULL columns without server defaults — must be provided in raw INSERTs.
_JOB_INSERT = (
    "INSERT INTO jobs "
    "(id, workspace_id, type, resource_type, resource_id, payload_version, "
    "payload_json, idempotency_key, state, safe_to_retry_after_crash, "
    "attempt, max_attempts, error_code, error_detail, created_at) "
    "VALUES (:id, 'ws_health', 'test', '', '', 1, "
    "'{}'::json, :idem_key, :state, false, "
    "0, 1, '', '', now())"
)


@unittest.skipUnless(
    OWNER_URL and API_URL,
    "set DOMAIN_OWNER_DATABASE_URL and DOMAIN_API_DATABASE_URL",
)
class HealthStatsFunctionTests(unittest.TestCase):
    """Prove marketai_health_stats_v1() returns correct aggregates."""

    def setUp(self) -> None:
        self.owner_engine = create_engine(OWNER_URL)
        self.api_engine = create_engine(API_URL)
        with self.owner_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO workspaces "
                "(id, name, default_currency, created_at, lifecycle, budget_currency) "
                "VALUES ('ws_health', 'health-test', 'USD', now(), 'active', 'USD') "
                "ON CONFLICT (id) DO NOTHING"
            ))

    def tearDown(self) -> None:
        with self.owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE workspace_id = 'ws_health'"))
            conn.execute(text("DELETE FROM workspaces WHERE id = 'ws_health'"))
        self.owner_engine.dispose()
        self.api_engine.dispose()

    def test_function_exists_and_returns_jsonb(self) -> None:
        with self.api_engine.connect() as conn:
            result = conn.execute(
                text("SELECT marketai_health_stats_v1()")
            ).scalar()
        self.assertIsNotNone(result)
        self.assertIn("job_queue_depth", result)
        self.assertIn("uncertain_jobs", result)
        self.assertIn("running_jobs", result)
        self.assertIn("oldest_heartbeat_age_seconds", result)
        self.assertIn("stale_operations", result)

    def test_empty_tables_returns_zeros(self) -> None:
        with self.api_engine.connect() as conn:
            result = conn.execute(
                text("SELECT marketai_health_stats_v1()")
            ).scalar()
        self.assertEqual(result["job_queue_depth"], 0)
        self.assertEqual(result["uncertain_jobs"], 0)
        self.assertEqual(result["running_jobs"], 0)
        self.assertEqual(result["oldest_heartbeat_age_seconds"], 0)
        self.assertEqual(result["stale_operations"], 0)

    def test_queued_job_increments_depth(self) -> None:
        with self.owner_engine.begin() as conn:
            conn.execute(text(_JOB_INSERT), {
                "id": "health_test_q", "idem_key": "health_idem_q", "state": "queued",
            })
        with self.api_engine.connect() as conn:
            result = conn.execute(
                text("SELECT marketai_health_stats_v1()")
            ).scalar()
        self.assertGreaterEqual(result["job_queue_depth"], 1)

    def test_uncertain_job_counted(self) -> None:
        with self.owner_engine.begin() as conn:
            conn.execute(text(_JOB_INSERT), {
                "id": "health_test_u", "idem_key": "health_idem_u", "state": "uncertain",
            })
        with self.api_engine.connect() as conn:
            result = conn.execute(
                text("SELECT marketai_health_stats_v1()")
            ).scalar()
        self.assertGreaterEqual(result["uncertain_jobs"], 1)

    def test_api_role_cannot_bypass_rls_directly(self) -> None:
        """The API role sees zero rows through RLS but health function works."""
        with self.owner_engine.begin() as conn:
            conn.execute(text(_JOB_INSERT), {
                "id": "health_test_rls", "idem_key": "health_idem_rls", "state": "queued",
            })
        with self.api_engine.connect() as conn:
            direct_count = conn.execute(
                text("SELECT count(*) FROM jobs")
            ).scalar()
            self.assertEqual(direct_count, 0, "API role must not see jobs through RLS")

            stats = conn.execute(
                text("SELECT marketai_health_stats_v1()")
            ).scalar()
            self.assertGreaterEqual(
                stats["job_queue_depth"], 1,
                "SECURITY DEFINER function must see jobs across tenants",
            )
