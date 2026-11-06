from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest


OWNER_URL = os.getenv("DOMAIN_OWNER_DATABASE_URL")
WORKER_URL = os.getenv("DOMAIN_WORKER_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_URL or not WORKER_URL,
    reason="requires migrated PostgreSQL owner and Worker roles",
)


def test_one_pending_fact_is_leased_to_only_one_worker() -> None:
    import psycopg

    workspace_id = "ws_fact_" + uuid.uuid4().hex[:12]
    event_id = "obf_" + uuid.uuid4().hex
    with psycopg.connect(OWNER_URL) as connection:
        connection.execute(
            """
            INSERT INTO workspaces (
              id, name, default_currency, authorization_epoch,
              entitlement_version, suspended, projection_version,
              created_at, lifecycle, budget_currency, is_platform_workspace
            ) VALUES (%s, %s, 'USD', 0, 0, false, 0, now(), 'active', 'USD', false)
            """,
            (workspace_id, workspace_id),
        )
        connection.execute(
            """
            INSERT INTO onboarding_fact_outbox (
              id, workspace_id, fact_type, source_resource_id, observed_at
            ) VALUES (%s, %s, 'resolution_preview.validated', %s, now())
            """,
            (event_id, workspace_id, "preview-" + uuid.uuid4().hex[:12]),
        )

    def claim(index: int):
        with psycopg.connect(WORKER_URL) as connection:
            return connection.execute(
                "SELECT event_id FROM marketai_claim_next_onboarding_fact(%s, 30)",
                (f"worker-{index}",),
            ).fetchone()

    with ThreadPoolExecutor(max_workers=6) as executor:
        claims = list(executor.map(claim, range(6)))
    assert [row[0] for row in claims if row] == [event_id]
