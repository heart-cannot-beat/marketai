"""#26: one fact-type vocabulary, the Truth fact families.

The ManualFactSet publish validation, the TruthResolutionPolicy publish
validation and the Truth adapters' `_build_fact_value` all accept the same
families, and each family has one value structure that the publish
validation checks and the adapter parses.
"""
from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.library import contracts
from app.library.contracts import (
    ENTRY_MANUAL_FACT_SET,
    ENTRY_TRUTH_RESOLUTION_POLICY,
    validate_content,
)
from app.library.fact_types import FACT_FAMILIES, FACT_VALUE_FIELDS
from app.library.manual_fact_import import _validate_facts
from app.localization import claim_facts, fact_sources
from app.localization.fact_sources import (
    AvailabilityFactValue,
    ControlledMockFactSourceAdapter,
    DataSourceConfig,
    DeliveryFactValue,
    DiscountFactValue,
    FactResolutionRequest,
    ManualFactSourceAdapter,
    MarketContext,
    MoneyFactValue,
    OfferContext,
    ProductClaimFactValue,
    ShippingFactValue,
    SourceMappingContext,
)
from app.localization.truth import _manual_fact_set_for_market

_FAMILIES = ("price", "availability", "discount", "shipping", "delivery", "product_claim")
_LEGACY_NAMES = ("tax", "shipping_availability", "delivery_time")

# One row per family, written to that family's value structure.
_VALUES: dict[str, dict] = {
    "price": {"amount": "19.99", "currency": "EUR"},
    "availability": {"available": True},
    "discount": {
        "discount_type": "fixed", "amount": "2.00", "currency": "EUR",
        "conditions": "Orders over 50 EUR",
    },
    "shipping": {
        "method": "standard", "cost": "4.90", "currency": "EUR",
        "delivery_days_min": 2, "delivery_days_max": 5,
    },
    "delivery": {"delivery_days_min": 1, "delivery_days_max": 3, "region": "DE"},
    "product_claim": {
        "claim_type": "certification", "claim_text": "CE certified",
        "evidence_ref": "https://docs.acme-charge.example/ce",
    },
}

_EXPECTED: dict[str, object] = {
    "price": MoneyFactValue(amount=Decimal("19.99"), currency="EUR"),
    "availability": AvailabilityFactValue(available=True),
    "discount": DiscountFactValue(
        discount_type="fixed", amount=Decimal("2.00"), currency="EUR",
        conditions="Orders over 50 EUR",
    ),
    "shipping": ShippingFactValue(
        method="standard", cost=Decimal("4.90"), currency="EUR",
        delivery_days_min=2, delivery_days_max=5,
    ),
    "delivery": DeliveryFactValue(delivery_days_min=1, delivery_days_max=3, region="DE"),
    "product_claim": ProductClaimFactValue(
        claim_type="certification", claim_text="CE certified",
        evidence_ref="https://docs.acme-charge.example/ce",
    ),
}


def _row(fact_type: str, fact_id: str = "fact-1", **values: object) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "fact_id": fact_id,
        "fact_type": fact_type,
        "country_code": "DE",
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "valid_to": (now + timedelta(days=30)).isoformat(),
        **values,
    }


def _fact_set(*rows: dict) -> dict:
    return {"data_source_entry_id": "lib_ds_acme", "facts": list(rows)}


def _policy(*fact_types: str, on_failure: str = "block") -> dict:
    return {
        "rules": [
            {
                "fact_type": fact_type,
                "source_priority": ["manual"],
                "allow_fallback": False,
                "max_staleness_seconds": 86400,
                "on_failure": on_failure,
            }
            for fact_type in fact_types
        ]
    }


def _blocking(entry_type: str, content: dict) -> set[tuple[str, str]]:
    report = validate_content(entry_type, content)
    return {(finding.code, finding.path) for finding in report.blocking}


def _resolve_manual(content: dict, *requested: str):
    manual = _manual_fact_set_for_market("mfs_acme_v1", content, "DE")
    request = FactResolutionRequest(
        requested_fact_types=tuple(requested),
        offer=OfferContext(offer_version_id="ov_acme", product_version_id="pv_acme"),
        market=MarketContext(market="DE", locale="de-DE", country="DE", currency="EUR"),
        source_mapping=SourceMappingContext(mapping_version_id=""),
        data_source=DataSourceConfig(data_source_version_id="dsv_acme"),
        manual_fact_set=manual,
        truth_data_mode="real",
        mock_scenario=None,
    )
    return ManualFactSourceAdapter().resolve(request)


class TestOneVocabulary(unittest.TestCase):
    """Guard: the three places that name fact types cannot drift apart."""

    def test_the_families_are_the_truth_fact_families(self):
        self.assertEqual(FACT_FAMILIES, frozenset(_FAMILIES))
        self.assertEqual(frozenset(FACT_VALUE_FIELDS), FACT_FAMILIES)

    def test_manual_set_policy_and_adapter_accept_the_same_types(self):
        candidates = set(_FAMILIES) | set(_LEGACY_NAMES) | {"price_claim", ""}
        manual_accepts = {
            fact_type for fact_type in candidates
            if ("fact_type_unknown", "facts[0].fact_type")
            not in _blocking(ENTRY_MANUAL_FACT_SET, _fact_set(_row(fact_type)))
        }
        policy_accepts = {
            fact_type for fact_type in candidates
            if validate_content(
                ENTRY_TRUTH_RESOLUTION_POLICY, _policy(fact_type, on_failure="skip")
            ).ok
        }
        adapter_dispatches = frozenset(fact_sources._FACT_VALUE_CLASSES)

        self.assertEqual(contracts.FACT_TYPES, FACT_FAMILIES)
        self.assertEqual(manual_accepts, set(FACT_FAMILIES))
        self.assertEqual(policy_accepts, set(FACT_FAMILIES))
        self.assertEqual(adapter_dispatches, FACT_FAMILIES)

    def test_each_value_class_has_exactly_its_family_fields(self):
        for family, value_class in fact_sources._FACT_VALUE_CLASSES.items():
            with self.subTest(family=family):
                self.assertEqual(
                    [field.name for field in dataclasses.fields(value_class)],
                    list(FACT_VALUE_FIELDS[family]),
                )

    def test_claim_mapping_only_names_fact_families(self):
        mapped = {
            fact_type
            for fact_types in claim_facts._CLAIM_FACT_TYPES.values()
            for fact_type in fact_types
        }
        self.assertLessEqual(mapped, set(FACT_FAMILIES))

    def test_the_mock_scenarios_only_hold_valid_family_values(self):
        for scenario, facts in ControlledMockFactSourceAdapter._SCENARIOS.items():
            for family, raw in facts.items():
                with self.subTest(scenario=scenario, family=family):
                    self.assertNotIsInstance(
                        fact_sources._build_fact_value(family, raw, scope_key="k"),
                        fact_sources.FactSourceFailure,
                    )


class TestEachFamilyPublishesAndResolves(unittest.TestCase):

    def test_a_row_written_to_the_family_structure_publishes_and_resolves(self):
        for family in _FAMILIES:
            with self.subTest(family=family):
                content = _fact_set(_row(family, **_VALUES[family]))
                self.assertEqual(_blocking(ENTRY_MANUAL_FACT_SET, content), set())

                result = _resolve_manual(content, family)
                self.assertEqual(result.failures, ())
                self.assertEqual([fact.value for fact in result.facts], [_EXPECTED[family]])

    def test_a_row_missing_a_field_is_blocked_at_that_field(self):
        for family in _FAMILIES:
            for missing in FACT_VALUE_FIELDS[family]:
                with self.subTest(family=family, missing=missing):
                    values = {k: v for k, v in _VALUES[family].items() if k != missing}
                    content = _fact_set(_row("price", **_VALUES["price"]), _row(
                        family, fact_id="fact-2", **values,
                    ))
                    self.assertEqual(
                        _blocking(ENTRY_MANUAL_FACT_SET, content),
                        {("fact_value_missing", f"facts[1].{missing}")},
                    )

    def test_a_badly_typed_value_is_blocked_at_its_field(self):
        cases = (
            ("price", {"amount": 19.99}, "fact_amount_not_string", "amount"),
            ("price", {"amount": "NaN"}, "fact_amount_invalid", "amount"),
            ("price", {"amount": "19,99"}, "fact_amount_parse", "amount"),
            ("discount", {"currency": "eur"}, "fact_currency", "currency"),
            ("shipping", {"cost": 4.9}, "fact_amount_not_string", "cost"),
            ("availability", {"available": "true"}, "fact_bool_invalid", "available"),
            ("delivery", {"delivery_days_min": "1"}, "fact_days_invalid", "delivery_days_min"),
            ("delivery", {"delivery_days_max": -1}, "fact_days_invalid", "delivery_days_max"),
            ("shipping", {"delivery_days_min": True}, "fact_days_invalid", "delivery_days_min"),
            ("shipping", {"delivery_days_min": 6}, "fact_days_range", "delivery_days_max"),
            ("product_claim", {"claim_text": "  "}, "fact_text_required", "claim_text"),
            ("discount", {"discount_type": 5}, "fact_text_required", "discount_type"),
        )
        for family, override, code, field in cases:
            with self.subTest(family=family, override=override):
                content = _fact_set(_row(family, **{**_VALUES[family], **override}))
                self.assertEqual(
                    _blocking(ENTRY_MANUAL_FACT_SET, content), {(code, f"facts[0].{field}")}
                )

    def test_tax_and_legacy_names_are_unknown_fact_types(self):
        for legacy in _LEGACY_NAMES:
            with self.subTest(fact_type=legacy):
                content = _fact_set(
                    _row("price", **_VALUES["price"]),
                    _row(legacy, fact_id="fact-2", rate="0.19"),
                )
                self.assertEqual(
                    _blocking(ENTRY_MANUAL_FACT_SET, content),
                    {("fact_type_unknown", "facts[1].fact_type")},
                )


class TestPolicyFactTypes(unittest.TestCase):

    def test_policy_with_an_unknown_fact_type_is_blocked(self):
        for unknown in (*_LEGACY_NAMES, "Price"):
            with self.subTest(fact_type=unknown):
                self.assertEqual(
                    _blocking(ENTRY_TRUTH_RESOLUTION_POLICY, _policy("price", unknown)),
                    {("trp_fact_type_unknown", "rules[1].fact_type")},
                )

    def test_policy_can_block_on_every_family(self):
        report = validate_content(ENTRY_TRUTH_RESOLUTION_POLICY, _policy(*_FAMILIES))
        self.assertTrue(report.ok, report.blocking)


class TestAdapterNeverRaisesOnARow(unittest.TestCase):
    """A published row the families do not cover fails that fact, not the request."""

    def test_legacy_rows_fail_as_unsupported(self):
        for legacy, values in (
            ("tax", {"rate": "0.19"}),
            ("shipping_availability", {"available": True}),
            ("delivery_time", {"amount": "3", "currency": "EUR"}),
        ):
            with self.subTest(fact_type=legacy):
                result = _resolve_manual(
                    _fact_set(_row("price", **_VALUES["price"]), _row(legacy, "fact-2", **values)),
                    "price", legacy,
                )
                self.assertEqual([fact.fact_type for fact in result.facts], ["price"])
                self.assertEqual(
                    [(f.fact_type, f.code, f.is_transient) for f in result.failures],
                    [(legacy, "fact_type_unsupported", False)],
                )

    def test_a_row_missing_fields_fails_as_invalid_value(self):
        # The pre-#26 publish validation never checked discount fields.
        result = _resolve_manual(
            _fact_set(_row("discount", amount="2.00", currency="EUR")), "discount",
        )
        self.assertEqual(result.facts, ())
        [failure] = result.failures
        self.assertEqual((failure.fact_type, failure.code), ("discount", "fact_value_invalid"))
        self.assertIn("discount_type", failure.message)
        self.assertIn("conditions", failure.message)

    def test_a_badly_typed_value_fails_as_invalid_value(self):
        result = _resolve_manual(
            _fact_set(_row("availability", available="false")), "availability",
        )
        self.assertEqual(result.facts, ())
        self.assertEqual([f.code for f in result.failures], ["fact_value_invalid"])


class TestManualFactImportVocabulary(unittest.TestCase):

    def test_import_accepts_every_family(self):
        rows = [_row(family, f"fact-{i}", **_VALUES[family]) for i, family in enumerate(_FAMILIES)]
        self.assertEqual(_validate_facts(rows), [])

    def test_import_rejects_tax(self):
        errors = _validate_facts([_row("tax", rate="0.19")])
        self.assertEqual(len(errors), 1)
        self.assertIn("facts[0]", errors[0])
        self.assertIn("fact_type", errors[0])


if __name__ == "__main__":
    unittest.main()
