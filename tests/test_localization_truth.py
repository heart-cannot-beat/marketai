"""Truth resolution module tests for Localization (#24, spec §2C).

Exercises `resolve_or_reuse_truth_package()` and `assess_truth_freshness()`
through their public interfaces with the two adapters shipped in Task 8
(`ManualFactSourceAdapter`, `ControlledMockFactSourceAdapter`). Direct table
access here is limited to setup fixtures and reading back persisted rows for
assertions -- resolution behavior itself always goes through the module
interface.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.adapters.persistence.database import Base
from app.adapters.persistence.models import (
    FactLibraryDependencyRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    MarketPolicyRecord,
    MarketPolicyVersionRecord,
    TruthPackageMarketRecord,
    TruthSourceAttemptRecord,
    WorkspaceRecord,
    new_id,
)
# These modules register additional tables on the shared `Base.metadata`
# (e.g. `credential_versions`, referenced by a FK elsewhere in the schema).
# `Base.metadata.create_all()` needs every such module imported first, or it
# cannot resolve those foreign keys -- same pattern as
# tests/test_generation_admission.py.
from app.entitlements.cost_models import SpendGuardrailRecord, SpendReservationRecord  # noqa: F401
from app.entitlements.credentials import CredentialVersionRecord  # noqa: F401
from app.entitlements.models import EntitlementProjectionRecord  # noqa: F401
from app.entitlements.usage_models import UsageEventRecord  # noqa: F401
from app.generation.models import GenerationDraftRecord  # noqa: F401
from app.localization.fact_sources import (
    ControlledMockFactSourceAdapter,
    ManualFactSet,
    ManualFactSourceAdapter,
)
from app.localization.platform import seed_beta_market_catalog
from app.localization.truth import (
    FactPolicy,
    MarketResolutionCommand,
    MarketResolutionInput,
    MixedTruthDataModeError,
    SourceStep,
    TruthResolutionCommand,
    TruthResolutionError,
    TruthResolutionResult,
    assess_truth_freshness,
    prepare_truth_resolution_command,
    resolve_or_reuse_truth_package,
)
from app.platform.tenancy import TenantContext

WS_ID = "ws_truth_test"

_ALL_MARKET_IDS = (
    "en-GB", "en-AU", "en-CA", "de-DE", "fr-FR", "es-ES", "it-IT", "ja-JP", "ko-KR",
)


def _make_version(
    session: Session, workspace_id: str, entry_type: str, *, content: dict | None = None
) -> str:
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
            content_json=content if content is not None else {},
            content_hash=f"hash-{version_id}",
            published_by="tester",
        )
    )
    session.flush()
    return version_id


class _TruthTestCase(unittest.TestCase):
    """Shared fixture: one Workspace, the nine-market platform catalog, and
    effective Product/Offer/TruthResolutionPolicy/DataSource versions."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(bind=self.engine)
        self.session = Session(bind=self.engine, expire_on_commit=False)

        self.session.add(
            WorkspaceRecord(id=WS_ID, name="Truth Test", lifecycle="active", budget_currency="USD")
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
        self.manual_fact_set_version_id = _make_version(self.session, WS_ID, "manual_fact_set")

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _market_policy(self, market_id: str):
        policy = self.session.query(MarketPolicyRecord).filter_by(market_id=market_id).one()
        version = (
            self.session.query(MarketPolicyVersionRecord)
            .filter_by(market_policy_id=policy.id, version_state="effective")
            .one()
        )
        return policy, version

    def _manual_market_command(
        self,
        market_id: str,
        *,
        facts: dict,
        fact_policies: tuple[FactPolicy, ...] | None = None,
        second_facts: dict | None = None,
        allow_fallback: bool = False,
    ) -> MarketResolutionCommand:
        """A market resolved by a single (or, with `second_facts`, a
        fallback pair of) Manual source(s)."""
        policy, version = self._market_policy(market_id)
        now = datetime.now(timezone.utc)
        window = (now - timedelta(days=1), now + timedelta(days=365))

        steps = [
            SourceStep(
                data_source_version_id=self.data_source_version_id,
                adapter=ManualFactSourceAdapter(),
                adapter_kind="manual",
                adapter_version="manual-v1",
                truth_data_mode="real",
                manual_fact_set=ManualFactSet(
                    version_id=self.manual_fact_set_version_id,
                    facts=facts,
                    valid_from=window[0],
                    valid_to=window[1],
                ),
                manual_fact_set_version_id=self.manual_fact_set_version_id,
            )
        ]
        if second_facts is not None:
            second_fact_set_version_id = _make_version(self.session, WS_ID, "manual_fact_set")
            steps.append(
                SourceStep(
                    data_source_version_id=self.data_source_version_id,
                    adapter=ManualFactSourceAdapter(),
                    adapter_kind="manual",
                    adapter_version="manual-v1",
                    truth_data_mode="real",
                    manual_fact_set=ManualFactSet(
                        version_id=second_fact_set_version_id,
                        facts=second_facts,
                        valid_from=window[0],
                        valid_to=window[1],
                    ),
                    manual_fact_set_version_id=second_fact_set_version_id,
                )
            )

        return MarketResolutionCommand(
            market=policy.market_id,
            locale=version.locale,
            country=version.country,
            currency=version.currency,
            market_policy_version_id=version.id,
            sources=tuple(steps),
            fact_policies=fact_policies or (FactPolicy("price"), FactPolicy("availability")),
            allow_fallback=allow_fallback,
        )

    def _command(self, markets: tuple[MarketResolutionCommand, ...], **overrides) -> TruthResolutionCommand:
        defaults = dict(
            product_version_id=self.product_version_id,
            offer_version_id=self.offer_version_id,
            truth_resolution_policy_version_id=self.policy_version_id,
            truth_data_mode="real",
            max_staleness_seconds=86400 * 30,
            resolution_rule_version="truth-rule-v1",
            markets=markets,
        )
        defaults.update(overrides)
        return TruthResolutionCommand(**defaults)


_COMPLETE_FACTS = {
    "price": {"amount": "19.99", "currency": "EUR"},
    "availability": {"available": True},
}

_UNAVAILABLE_FACTS = {
    "price": {"amount": "19.99", "currency": "EUR"},
    "availability": {"available": False},
}


class TestTruthResolution(_TruthTestCase):
    def test_resolve_creates_package_with_full_status(self):
        markets = tuple(
            self._manual_market_command(market_id, facts=_COMPLETE_FACTS)
            for market_id in _ALL_MARKET_IDS
        )
        command = self._command(markets)

        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        self.assertIsInstance(result, TruthResolutionResult)
        self.assertEqual(result.status, "full")
        self.assertFalse(result.reused)
        self.assertEqual(len(result.market_readiness), 9)
        self.assertEqual(set(result.market_readiness.keys()), set(_ALL_MARKET_IDS))
        for market_id, readiness in result.market_readiness.items():
            self.assertEqual(readiness, "ready", market_id)

        rows = (
            self.session.query(TruthPackageMarketRecord)
            .filter_by(truth_package_id=result.truth_package_id)
            .all()
        )
        self.assertEqual(len(rows), 9)

    def test_resolve_reuses_matching_canonical_hash(self):
        markets = (self._manual_market_command("en-GB", facts=_COMPLETE_FACTS),)
        command = self._command(markets)

        first = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()
        self.assertFalse(first.reused)

        second = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        self.assertTrue(second.reused)
        self.assertEqual(second.truth_package_id, first.truth_package_id)
        self.assertEqual(second.status, first.status)

    def test_degraded_when_permitted_fact_missing(self):
        facts = {
            "price": {"amount": "9.99", "currency": "EUR"},
            "availability": {"available": True},
            # "discount" intentionally absent from the manual fact set.
        }
        market = self._manual_market_command(
            "en-GB",
            facts=facts,
            fact_policies=(
                FactPolicy("price"),
                FactPolicy("availability"),
                FactPolicy("discount", on_failure="skip"),
            ),
        )
        command = self._command((market,))

        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.market_readiness["en-GB"], "degraded")

    def test_failed_when_no_executable_market(self):
        markets = (
            self._manual_market_command("en-GB", facts=_UNAVAILABLE_FACTS),
            self._manual_market_command("en-AU", facts=_UNAVAILABLE_FACTS),
        )
        command = self._command(markets)

        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.market_readiness["en-GB"], "unavailable")
        self.assertEqual(result.market_readiness["en-AU"], "unavailable")

    def test_mixed_real_mock_rejected(self):
        policy_gb, version_gb = self._market_policy("en-GB")
        now = datetime.now(timezone.utc)
        real_market = self._manual_market_command("en-GB", facts=_COMPLETE_FACTS)

        policy_au, version_au = self._market_policy("en-AU")
        mock_market = MarketResolutionCommand(
            market=policy_au.market_id,
            locale=version_au.locale,
            country=version_au.country,
            currency=version_au.currency,
            market_policy_version_id=version_au.id,
            sources=(
                SourceStep(
                    data_source_version_id=self.data_source_version_id,
                    adapter=ControlledMockFactSourceAdapter(),
                    adapter_kind="controlled_mock",
                    adapter_version="mock-v1",
                    truth_data_mode="mock",
                    mock_scenario="standard_9_market",
                    mock_version="mock-v1",
                ),
            ),
            fact_policies=(FactPolicy("price"), FactPolicy("availability")),
        )

        command = self._command((real_market, mock_market), truth_data_mode="real")

        with self.assertRaises(MixedTruthDataModeError):
            resolve_or_reuse_truth_package(self.session, self.ctx, command)

        # Rejection, not downgrade: nothing was persisted.
        self.session.rollback()
        self.assertEqual(self.session.query(TruthPackageMarketRecord).count(), 0)

    def test_price_on_failure_degrade_is_rejected(self):
        # Spec §2C step 6 / contracts.py PRICE_FORBIDDEN_ON_FAILURE: price
        # forbids `on_failure=degrade` (it permits `skip`).
        market = self._manual_market_command(
            "en-GB",
            facts=_COMPLETE_FACTS,
            fact_policies=(FactPolicy("price", on_failure="degrade"), FactPolicy("availability")),
        )
        command = self._command((market,))

        with self.assertRaises(TruthResolutionError):
            resolve_or_reuse_truth_package(self.session, self.ctx, command)

    def test_cross_workspace_product_version_is_rejected(self):
        # CLAUDE.md: negative tenant test wherever data/commands are
        # Workspace-scoped. A product_version_id from a different Workspace
        # must never resolve, even though the id string itself is well-formed.
        other_ws_id = "ws_truth_test_other_tenant"
        self.session.add(
            WorkspaceRecord(id=other_ws_id, name="Other Tenant", lifecycle="active", budget_currency="USD")
        )
        self.session.flush()
        other_workspace_product_version_id = _make_version(self.session, other_ws_id, "product")

        market = self._manual_market_command("en-GB", facts=_COMPLETE_FACTS)
        command = self._command((market,), product_version_id=other_workspace_product_version_id)

        with self.assertRaises(TruthResolutionError):
            resolve_or_reuse_truth_package(self.session, self.ctx, command)

        # Nothing was persisted under this Workspace as a result.
        self.assertEqual(
            self.session.query(TruthPackageMarketRecord).filter_by(workspace_id=WS_ID).count(), 0
        )


def _policy_content(source_priority: tuple[str, ...], *, fact_type: str = "price", on_failure: str = "skip") -> dict:
    return {
        "rules": [
            {
                "fact_type": fact_type,
                "source_priority": list(source_priority),
                "max_staleness_seconds": 86400,
                "on_failure": on_failure,
            }
        ]
    }


def _data_source_content(adapter_kind: str, *, execution_mode: str = "real", config: dict | None = None) -> dict:
    return {"adapter_kind": adapter_kind, "execution_mode": execution_mode, "config": config or {}}


def _manual_fact_set_content(*country_prices: tuple[str, str, str]) -> dict:
    """`country_prices` is (country_code, amount, currency) tuples."""
    return {
        "data_source_entry_id": "ds_entry_manual",
        "facts": [
            {
                "fact_id": f"fact_{i}",
                "fact_type": "price",
                "country_code": country_code,
                "valid_from": "2020-01-01",
                "valid_to": "2099-01-01",
                "amount": amount,
                "currency": currency,
            }
            for i, (country_code, amount, currency) in enumerate(country_prices)
        ],
    }


class TestPrepareTruthResolutionCommand(_TruthTestCase):
    """Exercises the policy-driven factory: adapter selection from
    `TruthResolutionPolicyVersion.rules[].source_priority`, the Manual and
    Controlled Mock adapters built from real `DataSourceVersion`/
    `ManualFactSetVersion` content_json shapes (per
    `app/library/contracts.py`), and the `adapter_not_enabled` path for an
    unregistered adapter_kind (e.g. "shopify")."""

    def test_controlled_mock_can_project_distinct_country_readiness(self):
        policy_version_id = _make_version(
            self.session,
            WS_ID,
            "truth_resolution_policy",
            content={
                "rules": [
                    {
                        "fact_type": fact_type,
                        "source_priority": ["mock"],
                        "max_staleness_seconds": 86400,
                        "on_failure": "skip" if fact_type == "discount" else "block",
                    }
                    for fact_type in ("price", "availability", "discount")
                ]
            },
        )
        data_source_version_id = _make_version(
            self.session,
            WS_ID,
            "data_source",
            content=_data_source_content(
                "mock",
                execution_mode="mock",
                config={
                    "mock_scenario": "standard_9_market",
                    "mock_scenarios_by_country": {
                        "GB": "complete_with_discount",
                        "CA": "standard_9_market",
                        "AU": "unavailable",
                    },
                },
            ),
        )
        market_versions = [
            self._market_policy(market)[1]
            for market in ("en-GB", "en-CA", "en-AU")
        ]

        command = prepare_truth_resolution_command(
            self.session,
            self.ctx,
            product_version_id=self.product_version_id,
            offer_version_id=self.offer_version_id,
            truth_resolution_policy_version_id=policy_version_id,
            data_source_version_ids=(data_source_version_id,),
            markets=tuple(
                MarketResolutionInput(market_policy_version_id=version.id)
                for version in market_versions
            ),
        )
        self.assertEqual(
            [market.sources[0].mock_scenario for market in command.markets],
            ["complete_with_discount", "standard_9_market", "unavailable"],
        )

        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        self.assertEqual(result.status, "degraded")
        self.assertEqual(
            result.market_readiness,
            {"en-GB": "ready", "en-CA": "degraded", "en-AU": "unavailable"},
        )

    def test_manual_adapter_selected_from_policy_and_mapping_dependency_recorded(self):
        policy_version_id = _make_version(
            self.session, WS_ID, "truth_resolution_policy", content=_policy_content(("manual",))
        )
        data_source_version_id = _make_version(
            self.session, WS_ID, "data_source", content=_data_source_content("manual")
        )
        _, market_policy_version = self._market_policy("en-GB")
        manual_fact_set_version_id = _make_version(
            self.session,
            WS_ID,
            "manual_fact_set",
            content=_manual_fact_set_content((market_policy_version.country, "19.99", "EUR")),
        )
        market_source_mapping_version_id = _make_version(
            self.session,
            WS_ID,
            "market_source_mapping",
            content={
                "offer_version_id": self.offer_version_id,
                "data_source_version_id": data_source_version_id,
                "external_object": {"kind": "product", "id": "shopify-prod-1"},
            },
        )

        command = prepare_truth_resolution_command(
            self.session,
            self.ctx,
            product_version_id=self.product_version_id,
            offer_version_id=self.offer_version_id,
            truth_resolution_policy_version_id=policy_version_id,
            data_source_version_ids=(data_source_version_id,),
            markets=(
                MarketResolutionInput(
                    market_policy_version_id=market_policy_version.id,
                    market_source_mapping_version_id=market_source_mapping_version_id,
                ),
            ),
            manual_fact_set_version_id=manual_fact_set_version_id,
        )
        self.assertEqual(command.truth_data_mode, "real")

        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        self.assertEqual(result.status, "full")
        self.assertEqual(result.market_readiness["en-GB"], "ready")

        mapping_edge = (
            self.session.query(FactLibraryDependencyRecord)
            .filter_by(
                truth_package_id=result.truth_package_id,
                dependency_kind="market_source_mapping_version",
            )
            .one()
        )
        self.assertEqual(mapping_edge.dependency_version_id, market_source_mapping_version_id)
        self.assertIsNotNone(mapping_edge.truth_fact_id)

        attempt = (
            self.session.query(TruthSourceAttemptRecord)
            .filter_by(truth_package_id=result.truth_package_id, fact_type="price", outcome="accepted")
            .one()
        )
        self.assertEqual(attempt.adapter_kind, "manual")
        self.assertEqual(attempt.market_source_mapping_version_id, market_source_mapping_version_id)

    def test_unregistered_adapter_produces_adapter_not_enabled(self):
        policy_version_id = _make_version(
            self.session, WS_ID, "truth_resolution_policy", content=_policy_content(("shopify",))
        )
        data_source_version_id = _make_version(
            self.session, WS_ID, "data_source", content=_data_source_content("shopify")
        )
        _, market_policy_version = self._market_policy("en-GB")

        command = prepare_truth_resolution_command(
            self.session,
            self.ctx,
            product_version_id=self.product_version_id,
            offer_version_id=self.offer_version_id,
            truth_resolution_policy_version_id=policy_version_id,
            data_source_version_ids=(data_source_version_id,),
            markets=(MarketResolutionInput(market_policy_version_id=market_policy_version.id),),
        )

        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        # No adapter can ever serve "shopify" in this ticket, so price stays
        # unresolved -- and price forbids landing on a degraded/ready market.
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.market_readiness["en-GB"], "failed")

        failed_attempt = (
            self.session.query(TruthSourceAttemptRecord)
            .filter_by(truth_package_id=result.truth_package_id, fact_type="price", outcome="failed")
            .one()
        )
        self.assertEqual(failed_attempt.failure_code, "adapter_not_enabled")
        self.assertEqual(failed_attempt.adapter_kind, "shopify")

        skipped_attempt = (
            self.session.query(TruthSourceAttemptRecord)
            .filter_by(truth_package_id=result.truth_package_id, fact_type="price", outcome="skipped")
            .one()
        )
        self.assertEqual(skipped_attempt.skip_code, "on_failure_skip")

    def test_divergent_source_priority_across_rules_is_rejected(self):
        policy_version_id = _make_version(
            self.session,
            WS_ID,
            "truth_resolution_policy",
            content={
                "rules": [
                    {
                        "fact_type": "price",
                        "source_priority": ["manual"],
                        "max_staleness_seconds": 86400,
                        "on_failure": "skip",
                    },
                    {
                        "fact_type": "discount",
                        "source_priority": ["mock", "manual"],
                        "max_staleness_seconds": 86400,
                        "on_failure": "skip",
                    },
                ]
            },
        )
        data_source_version_id = _make_version(
            self.session, WS_ID, "data_source", content=_data_source_content("manual")
        )
        _, market_policy_version = self._market_policy("en-GB")

        with self.assertRaises(TruthResolutionError):
            prepare_truth_resolution_command(
                self.session,
                self.ctx,
                product_version_id=self.product_version_id,
                offer_version_id=self.offer_version_id,
                truth_resolution_policy_version_id=policy_version_id,
                data_source_version_ids=(data_source_version_id,),
                markets=(MarketResolutionInput(market_policy_version_id=market_policy_version.id),),
                manual_fact_set_version_id="lv_unused",
            )


class TestAssessTruthFreshness(_TruthTestCase):
    def test_fresh_package_is_current(self):
        markets = (self._manual_market_command("en-GB", facts=_COMPLETE_FACTS),)
        command = self._command(markets)
        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        report = assess_truth_freshness(
            self.session, self.ctx, result.truth_package_id, at=datetime.now(timezone.utc)
        )
        self.assertTrue(report.is_current, report.reasons)
        self.assertEqual(report.reasons, ())

    def test_superseded_dependency_makes_package_not_current(self):
        markets = (self._manual_market_command("en-GB", facts=_COMPLETE_FACTS),)
        command = self._command(markets)
        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        offer_version = self.session.get(LibraryVersionRecord, self.offer_version_id)
        offer_version.state = "superseded"
        self.session.flush()
        self.session.commit()

        report = assess_truth_freshness(
            self.session, self.ctx, result.truth_package_id, at=datetime.now(timezone.utc)
        )
        self.assertFalse(report.is_current)
        self.assertTrue(
            any(reason.startswith("library_dependency_not_effective:offer_version") for reason in report.reasons),
            report.reasons,
        )

    def test_expired_valid_until_makes_package_not_current(self):
        markets = (self._manual_market_command("en-GB", facts=_COMPLETE_FACTS),)
        command = self._command(markets)
        result = resolve_or_reuse_truth_package(self.session, self.ctx, command)
        self.session.commit()

        far_future = datetime.now(timezone.utc) + timedelta(days=365 * 10)
        report = assess_truth_freshness(self.session, self.ctx, result.truth_package_id, at=far_future)
        self.assertFalse(report.is_current)
        self.assertIn("valid_until_expired", report.reasons)


if __name__ == "__main__":
    unittest.main()
