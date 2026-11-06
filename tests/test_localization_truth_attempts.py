"""TruthSourceAttempt persistence tests for Localization (#24, spec §3D).

`resolve_or_reuse_truth_package()` is exercised through its public
interface; assertions read back the persisted `TruthSourceAttemptRecord` /
`TruthFactRecord` rows it wrote, which is exactly the "provenance evidence"
carve-out spec §3A reserves for direct table access.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    LibraryEntryRecord,
    LibraryVersionRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    TruthFactRecord,
    TruthSourceAttemptRecord,
    WorkspaceRecord,
    new_id,
)
# See tests/test_localization_truth.py for why these otherwise-unused
# imports are required before `Base.metadata.create_all()`.
from app.entitlements.cost_models import SpendGuardrailRecord, SpendReservationRecord  # noqa: F401
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401
from app.entitlements.models import EntitlementProjectionRecord  # noqa: F401
from app.entitlements.usage_models import UsageEventRecord  # noqa: F401
from app.generation.models import GenerationDraftRecord  # noqa: F401
from app.localization.fact_sources import ManualFactSet, ManualFactSourceAdapter
from app.localization.platform import seed_beta_market_catalog
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    SourceStep,
    TruthResolutionCommand,
    resolve_or_reuse_truth_package,
)
from app.platform.tenancy import TenantContext

WS_ID = "ws_truth_attempts_test"


def _make_version(session: Session, workspace_id: str, entry_type: str) -> str:
    entry_id = new_id("le")
    session.add(
        LibraryEntryRecord(
            id=entry_id,
            workspace_id=workspace_id,
            entry_type=entry_type,
            display_name=entry_type,
            lifecycle="active",
        )
    )
    session.flush()
    version_id = new_id("lv")
    session.add(
        LibraryVersionRecord(
            id=version_id,
            workspace_id=workspace_id,
            entry_id=entry_id,
            entry_type=entry_type,
            version_no=1,
            content_json={},
            content_hash=f"hash-{version_id}",
            published_by="tester",
        )
    )
    session.flush()
    return version_id


class TestTruthSourceAttempts(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.session = Session(bind=self.engine, expire_on_commit=False)

        self.session.add(
            WorkspaceRecord(id=WS_ID, name="Attempts Test", lifecycle="active", budget_currency="USD")
        )
        self.session.flush()

        seed_beta_market_catalog(self.session)
        self.session.flush()

        self.ctx = TenantContext(
            workspace_id=WS_ID, role="owner", initiator_id="user_1", initiator_role="owner",
        )

        self.product_version_id = _make_version(self.session, WS_ID, "product")
        self.offer_version_id = _make_version(self.session, WS_ID, "offer")
        self.policy_version_id = _make_version(self.session, WS_ID, "truth_resolution_policy")
        self.data_source_version_id = _make_version(self.session, WS_ID, "data_source")

        policy = self.session.query(MarketPolicyRecord).filter_by(market_id="en-GB").one()
        self.market_policy_version = (
            self.session.query(MarketPolicyVersionRecord)
            .filter_by(market_policy_id=policy.id, version_state="effective")
            .one()
        )
        self.market_policy = policy

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _manual_step(self, facts: dict, *, manual_fact_set_version_id: str | None = None) -> SourceStep:
        manual_fact_set_version_id = manual_fact_set_version_id or _make_version(
            self.session, WS_ID, "manual_fact_set"
        )
        now = datetime.now(timezone.utc)
        return SourceStep(
            data_source_version_id=self.data_source_version_id,
            adapter=ManualFactSourceAdapter(),
            adapter_kind="manual",
            adapter_version="manual-v1",
            truth_data_mode="real",
            manual_fact_set=ManualFactSet(
                version_id=manual_fact_set_version_id,
                facts=facts,
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=365),
            ),
            manual_fact_set_version_id=manual_fact_set_version_id,
        )

    def _command(self, market: MarketResolutionCommand) -> TruthResolutionCommand:
        return TruthResolutionCommand(
            product_version_id=self.product_version_id,
            offer_version_id=self.offer_version_id,
            truth_resolution_policy_version_id=self.policy_version_id,
            truth_data_mode="real",
            max_staleness_seconds=86400 * 30,
            resolution_rule_version="truth-rule-v1",
            markets=(market,),
        )

    def test_accepted_attempt_has_evidence_hash(self):
        facts = {"price": {"amount": "9.99", "currency": "EUR"}, "availability": {"available": True}}
        market = MarketResolutionCommand(
            market=self.market_policy.market_id,
            locale=self.market_policy_version.locale,
            country=self.market_policy_version.country,
            currency=self.market_policy_version.currency,
            market_policy_version_id=self.market_policy_version.id,
            sources=(self._manual_step(facts),),
            fact_policies=(FactPolicy("price"), FactPolicy("availability")),
        )
        result = resolve_or_reuse_truth_package(self.session, self.ctx, self._command(market))
        self.session.commit()

        attempts = (
            self.session.query(TruthSourceAttemptRecord)
            .filter_by(truth_package_id=result.truth_package_id, outcome="accepted")
            .all()
        )
        self.assertEqual(len(attempts), 2)  # price + availability
        for attempt in attempts:
            self.assertIsNotNone(attempt.evidence_hash)
            self.assertIsNone(attempt.failure_code)
            self.assertIsNone(attempt.skip_code)

    def test_failed_attempt_has_failure_code(self):
        # "discount" is requested but absent from the manual fact set, and
        # there is no fallback source, so it fails outright at the sole
        # configured source before the on_failure policy is applied.
        facts = {"price": {"amount": "9.99", "currency": "EUR"}, "availability": {"available": True}}
        market = MarketResolutionCommand(
            market=self.market_policy.market_id,
            locale=self.market_policy_version.locale,
            country=self.market_policy_version.country,
            currency=self.market_policy_version.currency,
            market_policy_version_id=self.market_policy_version.id,
            sources=(self._manual_step(facts),),
            fact_policies=(
                FactPolicy("price"),
                FactPolicy("availability"),
                FactPolicy("discount", on_failure="skip"),
            ),
        )
        result = resolve_or_reuse_truth_package(self.session, self.ctx, self._command(market))
        self.session.commit()

        failed = (
            self.session.query(TruthSourceAttemptRecord)
            .filter_by(
                truth_package_id=result.truth_package_id,
                outcome="failed",
                fact_type="discount",
            )
            .one()
        )
        self.assertEqual(failed.failure_code, "fact_type_not_in_manual_set")
        self.assertIsNone(failed.evidence_hash)
        self.assertIsNone(failed.skip_code)

    def test_skipped_attempt_has_skip_code(self):
        facts = {"price": {"amount": "9.99", "currency": "EUR"}, "availability": {"available": True}}
        market = MarketResolutionCommand(
            market=self.market_policy.market_id,
            locale=self.market_policy_version.locale,
            country=self.market_policy_version.country,
            currency=self.market_policy_version.currency,
            market_policy_version_id=self.market_policy_version.id,
            sources=(self._manual_step(facts),),
            fact_policies=(
                FactPolicy("price"),
                FactPolicy("availability"),
                FactPolicy("discount", on_failure="skip"),
            ),
        )
        result = resolve_or_reuse_truth_package(self.session, self.ctx, self._command(market))
        self.session.commit()

        skipped = (
            self.session.query(TruthSourceAttemptRecord)
            .filter_by(
                truth_package_id=result.truth_package_id,
                outcome="skipped",
                fact_type="discount",
            )
            .one()
        )
        self.assertEqual(skipped.skip_code, "on_failure_skip")
        self.assertIsNone(skipped.evidence_hash)
        self.assertIsNone(skipped.failure_code)

    def test_fallback_creates_multiple_attempts(self):
        # First source has no "availability" entry; the second (fallback)
        # source does. `allow_fallback=True` lets resolution try it.
        first_facts = {"price": {"amount": "9.99", "currency": "EUR"}}
        second_facts = {"availability": {"available": True}}
        market = MarketResolutionCommand(
            market=self.market_policy.market_id,
            locale=self.market_policy_version.locale,
            country=self.market_policy_version.country,
            currency=self.market_policy_version.currency,
            market_policy_version_id=self.market_policy_version.id,
            sources=(self._manual_step(first_facts), self._manual_step(second_facts)),
            fact_policies=(FactPolicy("price"), FactPolicy("availability")),
            allow_fallback=True,
        )
        result = resolve_or_reuse_truth_package(self.session, self.ctx, self._command(market))
        self.session.commit()

        availability_attempts = (
            self.session.query(TruthSourceAttemptRecord)
            .filter_by(truth_package_id=result.truth_package_id, fact_type="availability")
            .order_by(TruthSourceAttemptRecord.priority_ordinal)
            .all()
        )
        self.assertEqual(len(availability_attempts), 2)
        self.assertEqual(availability_attempts[0].outcome, "failed")
        self.assertEqual(availability_attempts[0].priority_ordinal, 1)
        self.assertEqual(availability_attempts[1].outcome, "accepted")
        self.assertEqual(availability_attempts[1].priority_ordinal, 2)
        self.assertEqual(result.status, "full")

    def test_fact_references_accepted_attempt(self):
        facts = {"price": {"amount": "9.99", "currency": "EUR"}, "availability": {"available": True}}
        market = MarketResolutionCommand(
            market=self.market_policy.market_id,
            locale=self.market_policy_version.locale,
            country=self.market_policy_version.country,
            currency=self.market_policy_version.currency,
            market_policy_version_id=self.market_policy_version.id,
            sources=(self._manual_step(facts),),
            fact_policies=(FactPolicy("price"), FactPolicy("availability")),
        )
        result = resolve_or_reuse_truth_package(self.session, self.ctx, self._command(market))
        self.session.commit()

        price_fact = (
            self.session.query(TruthFactRecord)
            .filter_by(truth_package_id=result.truth_package_id, fact_type="price")
            .one()
        )
        accepted_attempt = self.session.get(TruthSourceAttemptRecord, price_fact.accepted_source_attempt_id)
        self.assertIsNotNone(accepted_attempt)
        self.assertEqual(accepted_attempt.outcome, "accepted")
        self.assertEqual(accepted_attempt.fact_type, "price")
        self.assertEqual(accepted_attempt.scope_key, price_fact.scope_key)
        self.assertEqual(accepted_attempt.evidence_hash, price_fact.evidence_hash)


# ---------------------------------------------------------------------------
# PostgreSQL-only: the append-only trigger itself lands with the Task 7
# migration and is not reproduced on SQLite (spec §3A reserves trigger
# evidence for the PostgreSQL-specific test layer). This proves it against a
# real migrated cluster when one is configured, and skips cleanly on a plain
# checkout / SQLite-only `make verify`, mirroring tests/test_postgres_rls.py.
# ---------------------------------------------------------------------------

_PG_URL = os.environ.get("DOMAIN_OWNER_DATABASE_URL")


@unittest.skipUnless(
    _PG_URL,
    "set DOMAIN_OWNER_DATABASE_URL against a cluster migrated to head "
    "(see tests/test_postgres_rls.py) to exercise the immutable trigger",
)
class TestImmutableTriggerRejectsUpdate(unittest.TestCase):
    def test_immutable_trigger_rejects_update(self):
        from sqlalchemy import text

        engine = create_engine(_PG_URL, future=True)
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT id FROM truth_source_attempts LIMIT 1")
                ).first()
                if row is None:
                    self.skipTest("no truth_source_attempts row available to probe")
                with self.assertRaises(Exception):
                    with conn.begin():
                        conn.execute(
                            text(
                                "UPDATE truth_source_attempts SET failure_code = 'x' "
                                "WHERE id = :id"
                            ),
                            {"id": row.id},
                        )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
