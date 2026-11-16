"""#7 against an isolated migrated PostgreSQL with the real API role.

SQLite cannot see a missing grant or a tenant leak. Two tests of one product
card are seeded as the schema owner; the API role then reads the reuse
candidates (the card lineage over ``marketing_revisions``, ``products``,
``assets`` and ``asset_versions``) and reuses another test's creative under
FORCE RLS. Another Workspace sees neither the round nor its candidates.

Run like ``tests.test_legacy_operations_pg``.
"""
from __future__ import annotations

import contextlib
import os
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.adapters.persistence.models import JobRecord, OperationRecord, utcnow
from app.marketing.workflow_models import MarketingCreativeReuseRecord

URLS = [os.getenv(key) for key in (
    "DOMAIN_OWNER_DATABASE_URL", "DOMAIN_API_DATABASE_URL", "DOMAIN_WORKER_DATABASE_URL",
)]


@unittest.skipUnless(
    all(URLS) and os.getenv("MARKETAI_TENANT_CONTEXT_SECRET"),
    "requires isolated PostgreSQL runtime roles",
)
class ReuseAcrossTheProductCardOnPostgresTests(unittest.TestCase):
    def setUp(self):
        self.engines = [create_engine(url) for url in URLS]
        for engine in self.engines:
            self.addCleanup(engine.dispose)
        self.sessions = [sessionmaker(bind=engine, expire_on_commit=False, autoflush=False) for engine in self.engines]

    def _as(self, index: int):
        stack = contextlib.ExitStack()
        stack.enter_context(patch("app.adapters.persistence.database.SessionLocal", self.sessions[index]))
        stack.enter_context(patch("app.platform.jobs.engine", self.engines[index]))
        return stack

    def _retire(self, workspace_id: str) -> None:
        # ``marketai_health_stats_v1`` counts open Jobs and Operations in every Workspace; later suites share the database.
        with self.engines[0].begin() as connection:
            connection.execute(update(JobRecord).where(
                JobRecord.workspace_id == workspace_id,
                JobRecord.state.in_(("queued", "retry_wait", "leased", "running", "uncertain")),
            ).values(state="cancelled", finished_at=utcnow(), lease_owner=None, lease_expires_at=None, heartbeat_at=None))
            connection.execute(update(OperationRecord).where(
                OperationRecord.workspace_id == workspace_id,
                OperationRecord.state.notin_(("succeeded", "failed", "partially_succeeded", "cancelled")),
            ).values(state="cancelled", updated_at=utcnow()))

    def test_the_api_role_lists_and_reuses_another_tests_creative_of_the_card_and_nothing_leaks(self):
        from tests.test_marketing_reuse import TestReuseWithinTheProductCard

        fixture = TestReuseWithinTheProductCard()
        with self._as(0):
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            mine = fixture.context
            self.addCleanup(self._retire, mine.workspace_id)
            first, first_round = fixture.started()
            _, second_round = fixture.started()
            stranger = fixture._make_context()
            self.addCleanup(self._retire, stranger.workspace_id)
        theirs = {a["asset_version_id"] for a in first_round["assets"]}
        with self._as(1):
            response = fixture.candidates(second_round, limit=50)
            self.assertEqual(response.status_code, 200, response.text)
            items = response.json()["items"]
            self.assertEqual({i["asset_version_id"] for i in items if i["relation"] == "other_test"}, theirs)
            self.assertEqual({i["source_project_id"] for i in items if i["relation"] == "other_test"}, {first["id"]})
            row = fixture.command(second_round, "creatives/reuse", {
                "asset_version_id": first_round["assets"][0]["asset_version_id"],
                "combination_id": second_round["assets"][0]["combination_id"]}, 201)
            self.assertIn(first_round["assets"][0]["asset_version_id"], {a["asset_version_id"] for a in row["assets"]})
            fixture.context = stranger
            for path in (f"/api/v1/marketing/rounds/{second_round['id']}/reuse-candidates",
                         f"/api/v1/marketing/rounds/{second_round['id']}"):
                refused = fixture.client.get(path)
                self.assertEqual((refused.status_code, refused.json()["code"]), (404, "round_not_found"), refused.text)
            fixture.context = mine
        with self.sessions[0]() as session:
            [link] = session.scalars(select(MarketingCreativeReuseRecord).where(
                MarketingCreativeReuseRecord.workspace_id == mine.workspace_id,
                MarketingCreativeReuseRecord.round_id == second_round["id"])).all()
        self.assertEqual((link.source_json["relation"], link.source_json["source_project_id"]), ("other_test", first["id"]))


if __name__ == "__main__":
    unittest.main()
