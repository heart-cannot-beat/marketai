"""#54: Truth reads the manual fact rows of the Batch's Offer, not every row of the market.

Before #54 `_manual_fact_set_for_market` kept every row of the market, so in a
market with two Offers a Batch could take the other Offer's price (the later
row of a fact type won). Now (contract §11.4, acceptance items 12 and 13):

- each row names an Offer *entry*; a Batch reads the rows whose entry is the
  one its OfferVersion belongs to, so republishing the Offer keeps the rows;
- a fact set with no row for the Batch's Offer in a market answers 422, as
  one with no row for the market always did;
- a fact set published before #54, whose rows name no Offer, is never
  rewritten. It serves a Batch only when no other Offer of the Workspace
  sells in the market; otherwise each fact type fails with
  `manual_fact_offer_ambiguous` for the policy's `on_failure` to decide, and
  the attempt records the rows it could not attribute.

Offers, DataSources, policies and fact sets publish through the Library
service (fact sets through the import code), Batches run on those published
Offers, and preparation goes through the v1 Localization API.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LocalizationBatchRecord, MarketPolicyVersionRecord, TruthPackageRecord
from app.library import contracts, service
from app.library.contracts import ENTRY_MANUAL_FACT_SET
from app.library.manual_fact_import import process_manual_fact_import
from app.localization.truth import assess_truth_freshness
from tests import test_localization_routes as legacy_routes
from tests.support.offers import publish_offer, publish_product, republish_offer
from tests.test_localization_manual_fact_row_validity import (
    RowValidityFixtures,
    _availability,
    _in_force,
    _price,
)

_TAGS = itertools.count(1)
MARKET = legacy_routes.MARKET_COUNTRY
_RULES = {"price": "block", "availability": "skip"}


def _published_before_54(owner, data_source_entry_id: str, rows: list[dict]) -> str:
    """Publish rows that name no Offer, the way publish accepted them before #54.

    Only the row-Offer requirement is lifted, for this one publish; the
    version is then an ordinary published fact set that Truth must not
    rewrite.
    """
    validate = contracts._VALIDATORS[ENTRY_MANUAL_FACT_SET]

    def before_54(content, findings):
        validate(content, findings)
        findings[:] = [finding for finding in findings if finding.code != "fact_offer_required"]

    with patch.dict(contracts._VALIDATORS, {ENTRY_MANUAL_FACT_SET: before_54}):
        entry = service.create_entry(
            owner, entry_type=ENTRY_MANUAL_FACT_SET, display_name=f"Facts before #54 {uuid.uuid4().hex[:6]}",
            content={"data_source_entry_id": data_source_entry_id, "facts": rows},
        )
        return service.publish(owner, entry["id"], entry["draft"]["revision"])["id"]


class OfferTruthFixtures(RowValidityFixtures):
    def setUp(self) -> None:
        super().setUp()
        self.tag = f"offer54-{next(_TAGS)}"
        _entry, self.product_version = publish_product(self.owner)

    def offer(self, sku: str, markets=("US", MARKET)) -> tuple[str, str]:
        """A published Offer of the fixture Product: ``(entry id, version id)``."""
        return publish_offer(self.owner, self.product_version, markets=markets, sku=sku)

    def batch_on(self, offer_version_id: str) -> dict:
        """A Batch in a `MARKET` market whose Winner ran on the published OfferVersion."""
        draft = self.harness._create_draft_via_http(
            f"{self.tag}-{next(_TAGS)}", offer_markets=("US", MARKET),
            library_versions=(self.product_version, offer_version_id),
        )
        draft["recipe_hash"] = self.harness._analysis_recipe_hash(draft["batch_id"])
        self.harness._seed_analysis(draft["winner"], draft["recipe_hash"], draft["batch_id"])
        with tenant_session(self.owner) as session:
            draft["country"] = session.get(MarketPolicyVersionRecord, draft["market_policy_version_id"]).country
        self.assertEqual(draft["country"], MARKET)
        return draft

    def imported_fact_set(self, data_source: str, rows: list[dict], *, default_offer: str | None = None) -> str:
        """Rows imported by the import code (with its default Offer) and published: the version id."""
        entry = process_manual_fact_import(
            self.owner, data_source_entry_id=data_source, upload_intent_id=f"ui_{uuid.uuid4().hex[:8]}",
            content_payload={"facts": rows}, default_offer_entry_id=default_offer,
        )
        self.assertEqual(entry["draft"]["validation"]["blocking"], [])
        return service.publish(self.owner, entry["id"], entry["draft"]["revision"])["id"]

    def is_current(self, truth_package_id: str) -> tuple[bool, tuple[str, ...]]:
        with tenant_session(self.owner) as session:
            report = assess_truth_freshness(session, self.owner, truth_package_id, at=datetime.now(timezone.utc))
            return report.is_current, report.reasons


def _evidence(fact_id: str, row: int, window: dict, *, offer: str | None, match: str, used: bool) -> dict:
    return {
        "fact_id": fact_id, "row": row,
        "valid_from": datetime.fromisoformat(window["valid_from"]).isoformat(),
        "valid_to": datetime.fromisoformat(window["valid_to"]).isoformat(),
        "status": "in_force", "used": used, "offer_entry_id": offer, "offer_match": match,
    }


class TestEachBatchReadsItsOwnOffersRows(OfferTruthFixtures):
    def test_two_offers_in_one_market_each_get_only_their_own_rows(self):
        offer_a, version_a = self.offer("ACME-65W")
        offer_b, version_b = self.offer("ACME-100W")
        batch_a, batch_b = self.batch_on(version_a), self.batch_on(version_b)
        data_source, policy = self._sources(_RULES)
        window = _in_force(hours=12)
        fact_set = self.imported_fact_set(data_source, [
            _price("price-a", MARKET, window, amount="19.99"),
            _availability("availability-a", MARKET, window),
            # The later price row of the market: before #54 Batch A took it.
            {**_price("price-b", MARKET, window, amount="24.99"), "offer_entry_id": offer_b},
        ], default_offer=offer_a)

        package_a = self._package(self._prepared(batch_a, policy, fact_set)["truth_package_id"])
        package_b = self._package(self._prepared(batch_b, policy, fact_set)["truth_package_id"])

        self.assertEqual(package_a["status"], "full")
        self.assertEqual(package_a["facts"]["price"]["value"], {"amount": "19.99", "currency": "USD"})
        self.assertIn("availability", package_a["facts"])
        self.assertEqual(package_a["attempts"]["price"]["rows"], [
            _evidence("price-a", 0, window, offer=offer_a, match="row", used=True),
        ])

        self.assertEqual(package_b["status"], "degraded")
        self.assertEqual(package_b["facts"]["price"]["value"], {"amount": "24.99", "currency": "USD"})
        self.assertNotIn("availability", package_b["facts"], "Offer A's availability is not Offer B's")
        self.assertEqual(package_b["attempts"]["price"]["rows"], [
            _evidence("price-b", 2, window, offer=offer_b, match="row", used=True),
        ])
        self.assertEqual(package_b["attempts"]["availability"]["failure_code"], "fact_type_not_in_manual_set")

    def test_a_fact_set_with_no_row_for_the_batchs_offer_answers_422(self):
        offer_a, _version_a = self.offer("ACME-65W")
        _offer_b, version_b = self.offer("ACME-100W")
        batch_b = self.batch_on(version_b)
        data_source, policy = self._sources(_RULES)
        fact_set = self.imported_fact_set(
            data_source, [_price("price-a", MARKET, _in_force(hours=12))], default_offer=offer_a,
        )

        response = self._start(batch_b, policy, fact_set)

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "truth_configuration_invalid")
        with tenant_session(self.owner) as session:
            self.assertIsNone(session.get(LocalizationBatchRecord, batch_b["batch_id"]).truth_package_id)


class TestAnOfferChangeReachesOnlyItsDependents(OfferTruthFixtures):
    """Acceptance item 12, on the Truth side."""

    def test_a_republished_offer_keeps_its_rows_and_stales_only_its_own_packages(self):
        offer_a, a_1 = self.offer("ACME-65W")
        offer_b, b_1 = self.offer("ACME-100W")
        data_source, policy = self._sources(_RULES)
        window = _in_force(hours=12)
        fact_set = self.imported_fact_set(data_source, [
            _price("price-a", MARKET, window, amount="19.99"),
            _availability("availability-a", MARKET, window),
        ], default_offer=offer_a)
        fact_set_b = self.imported_fact_set(data_source, [
            _price("price-b", MARKET, window, amount="24.99"),
            _availability("availability-b", MARKET, window),
        ], default_offer=offer_b)
        package_a_1 = self._prepared(self.batch_on(a_1), policy, fact_set)["truth_package_id"]
        package_b_1 = self._prepared(self.batch_on(b_1), policy, fact_set_b)["truth_package_id"]

        a_2 = republish_offer(self.owner, offer_a, {
            **service.get_version(self.owner, a_1)["content"],
            "landing_page_url": "https://acme-charge.example/offers/acme-65w-new",
        })

        self.assertEqual(self.is_current(package_b_1), (True, ()), "Offer B's package does not depend on Offer A")
        current, reasons = self.is_current(package_a_1)
        self.assertFalse(current)
        self.assertIn(f"library_dependency_not_effective:offer_version:{a_1}", reasons)

        # The rows name Offer A's entry: a Batch on A v2 reads them as they are.
        body = self._prepared(self.batch_on(a_2), policy, fact_set)
        package = self._package(body["truth_package_id"])
        self.assertEqual(package["status"], "full")
        self.assertEqual(package["facts"]["price"]["value"], {"amount": "19.99", "currency": "USD"})
        self.assertNotEqual(body["truth_package_id"], package_a_1)


class TestFactSetsPublishedBefore54(OfferTruthFixtures):
    def test_it_serves_the_only_offer_of_its_market(self):
        offer_a, version_a = self.offer("ACME-65W")
        self.offer("ACME-US-ONLY", markets=("US",))  # sells elsewhere: not an Offer of this market
        batch = self.batch_on(version_a)
        data_source, policy = self._sources(_RULES)
        window = _in_force(hours=12)
        fact_set = _published_before_54(self.owner, data_source, [
            _price("price", MARKET, window), _availability("availability", MARKET, window),
        ])

        package = self._package(self._prepared(batch, policy, fact_set)["truth_package_id"])

        self.assertEqual(package["status"], "full")
        self.assertEqual(package["facts"]["price"]["value"], {"amount": "19.99", "currency": "USD"})
        self.assertEqual(package["attempts"]["price"]["rows"], [
            _evidence("price", 0, window, offer=None, match="only_offer_in_market", used=True),
        ])

    def test_in_a_market_with_several_offers_each_fact_fails_with_a_typed_reason(self):
        _offer_a, version_a = self.offer("ACME-65W")
        offer_b, _version_b = self.offer("ACME-100W")
        service.archive_entry(  # archiving stops new selection, not its claim on old rows
            self.owner, offer_b, expected_revision=service.get_entry(self.owner, offer_b)["draft"]["revision"],
        )
        batch = self.batch_on(version_a)
        data_source, policy = self._sources(_RULES)
        window = _in_force(hours=12)
        fact_set = _published_before_54(self.owner, data_source, [
            _price("price", MARKET, window), _availability("availability", MARKET, window),
        ])

        response = self._start(batch, policy, fact_set)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["truth_status"], "failed")
        package = self._package(response.json()["truth_package_id"])
        self.assertEqual(package["facts"], {}, "no row is picked for the Batch")
        self.assertEqual(
            package["readiness_detail"],
            {"reason": "fact_missing_blocked", "blocked": ["price"], "missing": ["availability", "price"]},
        )
        self.assertEqual(package["attempts"], {
            "price": {
                "outcome": "failed", "failure_code": "manual_fact_offer_ambiguous", "skip_code": None,
                "rows": [_evidence("price", 0, window, offer=None, match="ambiguous", used=False)],
            },
            "availability": {
                "outcome": "failed", "failure_code": "manual_fact_offer_ambiguous", "skip_code": None,
                "rows": [_evidence("availability", 1, window, offer=None, match="ambiguous", used=False)],
            },
        })
        with tenant_session(self.owner) as session:
            stored = session.get(TruthPackageRecord, response.json()["truth_package_id"])
            self.assertEqual(stored.status, "failed")
        self.assertEqual(
            service.get_version(self.owner, fact_set)["content"]["facts"][0].get("offer_entry_id"), None,
            "the published version is not rewritten",
        )
