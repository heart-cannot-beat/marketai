import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from app.localization.fact_sources import (
    FactResolutionRequest, FactSourceAdapter, FactSourceResult,
    RawSourceFact, FactSourceFailure, MoneyFactValue, AvailabilityFactValue,
    ManualFactSourceAdapter, ControlledMockFactSourceAdapter,
    OfferContext, MarketContext, SourceMappingContext, DataSourceConfig,
    ManualFactSet,
)


class _AdapterContractMixin:
    """Shared contract: both Manual and Controlled Mock must pass."""
    adapter: FactSourceAdapter

    def _make_request(self, **overrides) -> FactResolutionRequest:
        defaults = dict(
            requested_fact_types=("price", "availability"),
            offer=OfferContext(offer_version_id="ov_1", product_version_id="pv_1"),
            market=MarketContext(market="de-DE", locale="de-DE", country="DE", currency="EUR"),
            source_mapping=SourceMappingContext(mapping_version_id="msm_1"),
            data_source=DataSourceConfig(data_source_version_id="dsv_1"),
            manual_fact_set=None,
            truth_data_mode="real",
            mock_scenario=None,
        )
        defaults.update(overrides)
        return FactResolutionRequest(**defaults)

    def test_returns_fact_source_result(self):
        result = self.adapter.resolve(self._make_request())
        self.assertIsInstance(result, FactSourceResult)

    def test_facts_are_raw_source_facts(self):
        result = self.adapter.resolve(self._make_request())
        for fact in result.facts:
            self.assertIsInstance(fact, RawSourceFact)

    def test_money_is_decimal_not_float(self):
        result = self.adapter.resolve(self._make_request())
        for fact in result.facts:
            if isinstance(fact.value, MoneyFactValue):
                self.assertIsInstance(fact.value.amount, Decimal)

    def test_availability_false_is_successful_fact(self):
        result = self.adapter.resolve(self._make_request(
            requested_fact_types=("availability",),
        ))
        avail_facts = [f for f in result.facts
                       if isinstance(f.value, AvailabilityFactValue)]
        if avail_facts:
            self.assertIsInstance(avail_facts[0].value.available, bool)

    def test_scope_key_is_concrete(self):
        result = self.adapter.resolve(self._make_request())
        for fact in result.facts:
            self.assertGreater(len(fact.scope_key), 0)
            self.assertNotIn("generic", fact.scope_key.lower())

    def test_evidence_hash_nonempty(self):
        result = self.adapter.resolve(self._make_request())
        for fact in result.facts:
            self.assertGreater(len(fact.evidence_hash), 0)


class TestManualAdapterContract(_AdapterContractMixin, unittest.TestCase):
    def setUp(self):
        self.adapter = ManualFactSourceAdapter()

    def _make_request(self, **overrides):
        overrides.setdefault("manual_fact_set", ManualFactSet(
            version_id="mfs_1",
            facts={"price": {"amount": "29.99", "currency": "EUR"}},
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc),
        ))
        overrides.setdefault("truth_data_mode", "real")
        return super()._make_request(**overrides)


class TestControlledMockAdapterContract(_AdapterContractMixin, unittest.TestCase):
    def setUp(self):
        self.adapter = ControlledMockFactSourceAdapter()

    def _make_request(self, **overrides):
        overrides.setdefault("truth_data_mode", "mock")
        overrides.setdefault("mock_scenario", "standard_9_market")
        return super()._make_request(**overrides)

    def test_deterministic_for_same_input(self):
        req = self._make_request()
        r1 = self.adapter.resolve(req)
        r2 = self.adapter.resolve(req)
        self.assertEqual(
            [f.evidence_hash for f in r1.facts],
            [f.evidence_hash for f in r2.facts],
        )

    def test_different_scenario_different_output(self):
        r1 = self.adapter.resolve(self._make_request(mock_scenario="standard_9_market"))
        r2 = self.adapter.resolve(self._make_request(mock_scenario="unavailable"))
        self.assertNotEqual(
            [f.evidence_hash for f in r1.facts],
            [f.evidence_hash for f in r2.facts],
        )
