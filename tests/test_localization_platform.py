"""Platform-scoped ORM records and nine-market seed for Localization (#24).

These tables are platform-scoped (no ``workspace_id``): Market Policy is a
platform-global catalog, not Workspace data, so it is deliberately outside
the tenant boundary that ``tenant_session``/RLS enforce elsewhere.
"""
from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    MarketPolicyVersionEventRecord,
    new_id,
    utcnow,
)


class TestMarketPolicyRecords(unittest.TestCase):
    def test_market_policy_table_name(self):
        self.assertEqual(MarketPolicyRecord.__tablename__, "market_policies")

    def test_market_policy_version_table_name(self):
        self.assertEqual(
            MarketPolicyVersionRecord.__tablename__, "market_policy_versions"
        )

    def test_market_policy_version_event_table_name(self):
        self.assertEqual(
            MarketPolicyVersionEventRecord.__tablename__,
            "market_policy_version_events",
        )


BETA_MARKETS = {
    "en-GB": ("en-GB", "GB", "GBP"),
    "en-AU": ("en-AU", "AU", "AUD"),
    "en-CA": ("en-CA", "CA", "CAD"),
    "de-DE": ("de-DE", "DE", "EUR"),
    "fr-FR": ("fr-FR", "FR", "EUR"),
    "es-ES": ("es-ES", "ES", "EUR"),
    "it-IT": ("it-IT", "IT", "EUR"),
    "ja-JP": ("ja-JP", "JP", "JPY"),
    "ko-KR": ("ko-KR", "KR", "KRW"),
}


class TestNineMarketSeed(unittest.TestCase):
    """Exercises the seed against an isolated in-memory engine.

    ``app.adapters.persistence.database.platform_session`` binds to the
    process-wide ``engine`` (the real, migration-managed database). Task 1
    intentionally ships no Alembic migration for the three Localization
    tables -- that lands with the domain/API work in a later task -- so
    there is no migrated schema for ``platform_session`` to see yet. A
    private engine with ``Base.metadata.create_all`` scoped to just these
    three tables (the same pattern ``tests/test_generation_admission.py``
    uses for isolated ORM tests) proves the records and the seed function
    independently of that still-pending migration.
    """

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            bind=self.engine,
            tables=[
                MarketPolicyRecord.__table__,
                MarketPolicyVersionRecord.__table__,
                MarketPolicyVersionEventRecord.__table__,
            ],
        )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_seed_creates_nine_policies(self):
        from app.localization.platform import seed_beta_market_catalog

        with Session(bind=self.engine, expire_on_commit=False) as session:
            seed_beta_market_catalog(session)
            session.commit()

            policies = session.query(MarketPolicyRecord).all()
            self.assertEqual(len(policies), 9)
            market_ids = {p.market_id for p in policies}
            self.assertEqual(market_ids, set(BETA_MARKETS.keys()))
            for p in policies:
                locale, country, currency = BETA_MARKETS[p.market_id]
                self.assertEqual(p.locale, locale)
                self.assertEqual(p.country, country)
                self.assertEqual(p.currency, currency)

    def test_seed_creates_one_effective_version_and_published_event_each(self):
        from app.localization.platform import seed_beta_market_catalog

        with Session(bind=self.engine, expire_on_commit=False) as session:
            seed_beta_market_catalog(session)
            session.commit()

            versions = session.query(MarketPolicyVersionRecord).all()
            events = session.query(MarketPolicyVersionEventRecord).all()
            self.assertEqual(len(versions), 9)
            self.assertEqual(len(events), 9)
            for v in versions:
                self.assertEqual(v.version_number, 1)
                self.assertEqual(v.version_state, "effective")
            for e in events:
                self.assertEqual(e.event_type, "published")
                self.assertIsNone(e.previous_state)
                self.assertEqual(e.current_state, "effective")
                self.assertEqual(e.sequence, 1)

    def test_seed_is_idempotent(self):
        from app.localization.platform import seed_beta_market_catalog

        with Session(bind=self.engine, expire_on_commit=False) as session:
            seed_beta_market_catalog(session)
            session.commit()
            seed_beta_market_catalog(session)
            session.commit()

            self.assertEqual(session.query(MarketPolicyRecord).count(), 9)
            self.assertEqual(session.query(MarketPolicyVersionRecord).count(), 9)
            self.assertEqual(
                session.query(MarketPolicyVersionEventRecord).count(), 9
            )
