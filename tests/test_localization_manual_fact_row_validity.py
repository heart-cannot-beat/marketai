"""#60: Truth judges a Manual Fact Set's validity row by row.

Before #60 `_manual_fact_set_for_market` took the intersection of every row's
window in a market (latest `valid_from`, earliest `valid_to`) as the window of
the whole set, so one expired or not-yet-valid row made every fact of that
market `manual_fact_set_expired`.

Now (contract §7.3, §7.4):

- resolution leaves out only the rows not in force at resolution time; a fact
  family left without a row in force follows its rule's `on_failure`;
- the TruthPackage's `valid_until` is the earliest end of the rows actually
  used;
- each manual source attempt records every row of its fact family for the
  market, with its window and whether it was in force and used
  (`requested_context_json.manual_fact_rows`). Packages resolved earlier are
  never rewritten.

Every DataSource, policy and fact set goes through `service.publish`, and
preparation through the v1 Localization API.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    LocalizationBatchRecord,
    MarketPolicyVersionRecord,
    TruthFactRecord,
    TruthPackageMarketRecord,
    TruthPackageRecord,
    TruthSourceAttemptRecord,
)
from app.library import service
from app.library.contracts import ENTRY_MANUAL_FACT_SET
from tests import test_localization_routes as legacy_routes
from tests.support.library_fixtures import LibraryFixtures
from tests.support.truth_sources import publish_data_source, publish_truth_policy, truth_policy_content

_TAGS = itertools.count(1)
_MANUAL_SOURCE = {"adapter_kind": "manual", "execution_mode": "real"}

EXPIRED = {"valid_from": "2020-01-01", "valid_to": "2020-12-31"}
NOT_YET = {"valid_from": "2099-01-01", "valid_to": "2099-12-31"}
EXPIRED_WINDOW = ("2020-01-01T00:00:00+00:00", "2020-12-31T00:00:00+00:00")
NOT_YET_WINDOW = ("2099-01-01T00:00:00+00:00", "2099-12-31T00:00:00+00:00")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _in_force(hours: int) -> dict:
    """A window that started yesterday and ends ``hours`` from now.

    The fixture MarketPolicy bounds a fact at one day, so a row ending
    within the day sets the fact's `valid_until`.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "valid_from": (now - timedelta(days=1)).isoformat(),
        "valid_to": (now + timedelta(hours=hours)).isoformat(),
    }


def _row(fact_id: str, fact_type: str, country: str, window: dict, **values: object) -> dict:
    return {
        "fact_id": fact_id,
        "fact_type": fact_type,
        "country_code": country,
        **window,
        "evidence_source": "Acme Charge price list",
        **values,
    }


def _price(fact_id: str, country: str, window: dict, amount: str = "19.99") -> dict:
    return _row(fact_id, "price", country, window, amount=amount, currency="USD")


def _availability(fact_id: str, country: str, window: dict) -> dict:
    return _row(fact_id, "availability", country, window, available=True)


def _shipping(fact_id: str, country: str, window: dict) -> dict:
    return _row(
        fact_id, "shipping", country, window,
        method="standard", cost="4.90", currency="USD", delivery_days_min=2, delivery_days_max=5,
    )


def _evidence(fact_id: str, row: int, window: tuple[str, str], status: str, used: bool, offer: str) -> dict:
    """One row as the attempt records it; each row names the Batch's Offer (#54)."""
    return {
        "fact_id": fact_id, "row": row, "valid_from": window[0], "valid_to": window[1],
        "status": status, "used": used, "offer_entry_id": offer, "offer_match": "row",
    }


def _of_offer(rows: list[dict], offer: str) -> list[dict]:
    return [{**row, "offer_entry_id": offer} for row in rows]


def _window_of(row: dict) -> tuple[str, str]:
    return (
        datetime.fromisoformat(row["valid_from"]).isoformat(),
        datetime.fromisoformat(row["valid_to"]).isoformat(),
    )


class RowValidityFixtures(LibraryFixtures):
    def setUp(self) -> None:
        self.harness = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.owner = self.harness.ctx
        self.client = self.harness.client
        self.tag = f"row60-{next(_TAGS)}"

    # ------------------------------------------------------------------
    # Library publish and the v1 Localization API only
    # ------------------------------------------------------------------

    def _sources(self, rules: dict[str, str], ctx=None) -> tuple[str, str]:
        """A published manual DataSource and a policy listing it: ``(data source entry id, policy version id)``."""
        ctx = ctx or self.owner
        tag = f"{self.tag}-{next(_TAGS)}"
        data_source_entry_id, _version_id = publish_data_source(ctx, f"Manual facts {tag}", _MANUAL_SOURCE)
        _policy_entry_id, policy_version_id = publish_truth_policy(
            ctx, f"Truth policy {tag}",
            truth_policy_content([data_source_entry_id], rules, max_staleness_seconds=86400 * 30),
        )
        return data_source_entry_id, policy_version_id

    def _fact_set(
        self, data_source_entry_id: str, rows: list[dict], *, offer: str, ctx=None
    ) -> tuple[str, str]:
        """Create and publish a fact set whose rows name ``offer``: ``(entry id, version id)``.

        Rows that already ended are a publish warning (#60), acknowledged here.
        """
        ctx = ctx or self.owner
        entry = service.create_entry(
            ctx, entry_type=ENTRY_MANUAL_FACT_SET, display_name=f"Facts {self.tag}-{next(_TAGS)}",
            content={"data_source_entry_id": data_source_entry_id, "facts": _of_offer(rows, offer)},
        )
        version = service.publish(
            ctx, entry["id"], entry["draft"]["revision"], acknowledged_warnings=["fact_expired"],
        )
        return entry["id"], version["id"]

    def _republish_fact_set(
        self, entry_id: str, data_source_entry_id: str, rows: list[dict], *, offer: str
    ) -> str:
        revision = service.get_entry(self.owner, entry_id)["draft"]["revision"]
        draft = service.save_draft(
            self.owner, entry_id,
            {"data_source_entry_id": data_source_entry_id, "facts": _of_offer(rows, offer)},
            expected_revision=revision,
        )
        return service.publish(
            self.owner, entry_id, draft["revision"], acknowledged_warnings=["fact_expired"],
        )["id"]

    def _batch(self, harness=None) -> dict:
        """A Batch whose Offer sells in the Batch's market, so fact rows can name it (#54)."""
        harness = harness or self.harness
        draft = harness._create_draft_via_http(
            f"{self.tag}-{next(_TAGS)}", offer_markets=("US", legacy_routes.MARKET_COUNTRY),
        )
        draft["recipe_hash"] = harness._analysis_recipe_hash(draft["batch_id"])
        harness._seed_analysis(draft["winner"], draft["recipe_hash"], draft["batch_id"])
        with tenant_session(harness.ctx) as session:
            draft["country"] = session.get(
                MarketPolicyVersionRecord, draft["market_policy_version_id"]
            ).country
        return draft

    def _start(self, draft: dict, policy_version_id: str, fact_set_version_id: str, client=None):
        client = client or self.client
        batch_id = draft["batch_id"]
        revision = client.get(f"/api/v1/localization/batches/{batch_id}").json()["revision"]
        return client.post(
            f"/api/v1/localization/batches/{batch_id}/start-preparation",
            json={
                "truth_resolution_policy_version_id": policy_version_id,
                "manual_fact_set_version_id": fact_set_version_id,
                "analysis_recipe_hash": draft["recipe_hash"],
                "analysis_provider": "mock-vision",
                "analysis_model": "mock-vision-1",
                "analysis_adapter_version": "mock-v1",
            },
            headers={"Idempotency-Key": f"{batch_id}-start", "If-Match": str(revision)},
        )

    def _prepared(self, draft: dict, policy_version_id: str, fact_set_version_id: str) -> dict:
        response = self._start(draft, policy_version_id, fact_set_version_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _package(self, truth_package_id: str) -> dict:
        """Everything the package froze, in a form two reads can compare."""
        with tenant_session(self.owner) as session:
            package = session.get(TruthPackageRecord, truth_package_id)
            market = session.query(TruthPackageMarketRecord).filter_by(
                workspace_id=self.owner.workspace_id, truth_package_id=truth_package_id,
            ).one()
            facts = session.query(TruthFactRecord).filter_by(
                workspace_id=self.owner.workspace_id, truth_package_id=truth_package_id,
            ).all()
            attempts = session.query(TruthSourceAttemptRecord).filter_by(
                workspace_id=self.owner.workspace_id, truth_package_id=truth_package_id,
            ).order_by(TruthSourceAttemptRecord.fact_type, TruthSourceAttemptRecord.priority_ordinal).all()
            return {
                "status": package.status,
                "valid_until": _aware(package.valid_until),
                "readiness": market.readiness,
                "readiness_detail": market.readiness_detail_json,
                "facts": {
                    fact.fact_type: {
                        "value": fact.value_json,
                        "valid_until": _aware(fact.valid_until),
                        "source_valid_until": _aware(fact.source_valid_until),
                    }
                    for fact in facts
                },
                "attempts": {
                    attempt.fact_type: {
                        "outcome": attempt.outcome,
                        "failure_code": attempt.failure_code,
                        "skip_code": attempt.skip_code,
                        "rows": attempt.requested_context_json.get("manual_fact_rows"),
                    }
                    for attempt in attempts
                    if attempt.adapter_kind == "manual"
                },
                "skips": sorted(
                    (attempt.fact_type, attempt.skip_code)
                    for attempt in attempts if attempt.outcome == "skipped"
                ),
            }


class TestRowsAreJudgedOneByOne(RowValidityFixtures):
    def test_an_expired_row_leaves_the_rows_still_in_force_usable(self):
        draft = self._batch()
        country, offer = draft["country"], draft["offer_entry_id"]
        data_source, policy = self._sources({"price": "block", "availability": "skip", "shipping": "skip"})
        price_now, availability_now = _in_force(hours=6), _in_force(hours=12)
        rows = [
            _price("price-now", country, price_now),
            _availability("availability-now", country, availability_now),
            # Listed after the current price: before #60 the last row of a
            # family won, so this expired price shadowed the one in force.
            _price("price-old", country, EXPIRED, amount="24.99"),
            _shipping("shipping-old", country, EXPIRED),
        ]
        _entry_id, fact_set = self._fact_set(data_source, rows, offer=offer)

        body = self._prepared(draft, policy, fact_set)

        self.assertEqual(body["truth_status"], "degraded")
        package = self._package(body["truth_package_id"])
        self.assertEqual(package["readiness"], "degraded")
        self.assertEqual(
            package["readiness_detail"], {"reason": "fact_missing_permitted", "missing": ["shipping"]},
        )
        self.assertEqual(package["skips"], [("shipping", "on_failure_skip")])

        self.assertEqual(package["facts"]["price"]["value"], {"amount": "19.99", "currency": "USD"})
        price_ends = datetime.fromisoformat(price_now["valid_to"])
        availability_ends = datetime.fromisoformat(availability_now["valid_to"])
        self.assertEqual(package["facts"]["price"]["source_valid_until"], price_ends)
        self.assertEqual(package["facts"]["availability"]["source_valid_until"], availability_ends)
        # Only the rows used bound the package: the earliest of their ends.
        self.assertEqual(package["valid_until"], price_ends)

        self.assertEqual(package["attempts"]["price"]["outcome"], "accepted")
        self.assertEqual(package["attempts"]["price"]["rows"], [
            _evidence("price-now", 0, _window_of(rows[0]), "in_force", True, offer),
            _evidence("price-old", 2, EXPIRED_WINDOW, "expired", False, offer),
        ])
        self.assertEqual(package["attempts"]["availability"]["rows"], [
            _evidence("availability-now", 1, _window_of(rows[1]), "in_force", True, offer),
        ])
        self.assertEqual(package["attempts"]["shipping"], {
            "outcome": "failed",
            "failure_code": "manual_fact_expired",
            "skip_code": None,
            "rows": [_evidence("shipping-old", 3, EXPIRED_WINDOW, "expired", False, offer)],
        })

    def test_a_row_not_yet_in_force_is_left_out(self):
        draft = self._batch()
        country, offer = draft["country"], draft["offer_entry_id"]
        data_source, policy = self._sources({"price": "block", "availability": "skip"})
        price_now = _in_force(hours=6)
        rows = [
            _price("price-now", country, price_now),
            _price("price-next", country, NOT_YET, amount="17.99"),
            _availability("availability-next", country, NOT_YET),
        ]
        _entry_id, fact_set = self._fact_set(data_source, rows, offer=offer)

        body = self._prepared(draft, policy, fact_set)

        self.assertEqual(body["truth_status"], "degraded")
        package = self._package(body["truth_package_id"])
        self.assertEqual(
            package["readiness_detail"], {"reason": "fact_missing_permitted", "missing": ["availability"]},
        )
        self.assertEqual(package["facts"]["price"]["value"], {"amount": "19.99", "currency": "USD"})
        self.assertEqual(package["valid_until"], datetime.fromisoformat(price_now["valid_to"]))
        self.assertEqual(package["attempts"]["price"]["rows"], [
            _evidence("price-now", 0, _window_of(rows[0]), "in_force", True, offer),
            _evidence("price-next", 1, NOT_YET_WINDOW, "not_yet_valid", False, offer),
        ])
        self.assertEqual(package["attempts"]["availability"], {
            "outcome": "failed",
            "failure_code": "manual_fact_not_yet_valid",
            "skip_code": None,
            "rows": [_evidence("availability-next", 2, NOT_YET_WINDOW, "not_yet_valid", False, offer)],
        })

    def test_with_no_row_in_force_each_family_follows_its_on_failure(self):
        cases = [
            (
                {"price": "block", "availability": "skip"},
                {"reason": "fact_missing_blocked", "blocked": ["price"], "missing": ["availability", "price"]},
            ),
            (
                {"price": "skip", "availability": "degrade"},
                {"reason": "price_unresolved", "missing": ["availability", "price"]},
            ),
        ]
        for rules, detail in cases:
            with self.subTest(rules=rules):
                draft = self._batch()
                country = draft["country"]
                data_source, policy = self._sources(rules)
                _entry_id, fact_set = self._fact_set(data_source, [
                    _price("price-old", country, EXPIRED),
                    _availability("availability-old", country, EXPIRED),
                ], offer=draft["offer_entry_id"])

                response = self._start(draft, policy, fact_set)

                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["truth_status"], "failed")
                package = self._package(body["truth_package_id"])
                self.assertEqual((package["readiness"], package["readiness_detail"]), ("failed", detail))
                self.assertEqual(package["facts"], {})
                self.assertEqual(
                    {fact_type: attempt["failure_code"] for fact_type, attempt in package["attempts"].items()},
                    {"price": "manual_fact_expired", "availability": "manual_fact_expired"},
                )


class TestEarlierPackagesStayAsResolved(RowValidityFixtures):
    def test_a_package_resolved_earlier_is_not_rewritten(self):
        data_source, policy = self._sources({"price": "block", "availability": "skip"})
        first = self._batch()
        country = first["country"]
        price_now = _in_force(hours=6)
        entry_id, version_1 = self._fact_set(data_source, [
            _price("price-now", country, price_now),
            _availability("availability-now", country, _in_force(hours=12)),
        ], offer=first["offer_entry_id"])
        first_body = self._prepared(first, policy, version_1)
        self.assertEqual(first_body["truth_status"], "full")
        resolved = self._package(first_body["truth_package_id"])

        # The fact set's next version holds an expired availability row, for
        # the second Batch's Offer (each fixture Batch has its own Offer).
        second = self._batch()
        version_2 = self._republish_fact_set(entry_id, data_source, [
            _price("price-now", country, price_now),
            _availability("availability-old", country, EXPIRED),
        ], offer=second["offer_entry_id"])
        second_country = second["country"]
        self.assertEqual(second_country, country, "both fixture markets are RT* markets")
        second_body = self._prepared(second, policy, version_2)

        self.assertEqual(second_body["truth_status"], "degraded")
        self.assertNotEqual(second_body["truth_package_id"], first_body["truth_package_id"])
        self.assertEqual(self._package(first_body["truth_package_id"]), resolved)
        with tenant_session(self.owner) as session:
            self.assertEqual(
                session.get(LocalizationBatchRecord, first["batch_id"]).truth_package_id,
                first_body["truth_package_id"],
            )


class TestTenantBoundary(RowValidityFixtures):
    def test_another_workspaces_fact_set_is_not_resolved(self):
        own_draft = self._batch()
        data_source, _policy = self._sources({"price": "block"})
        _entry_id, own_fact_set = self._fact_set(data_source, [
            _price("price-now", own_draft["country"], _in_force(hours=6)),
        ], offer=own_draft["offer_entry_id"])

        # A second Workspace, with its own published DataSource and policy,
        # names the first Workspace's fact set version.
        other = legacy_routes.TestLocalizationRoutes(methodName="test_create_draft")
        other.setUp()
        self.addCleanup(other.doCleanups)
        self.assertNotEqual(other.ctx.workspace_id, self.owner.workspace_id)
        _other_source, other_policy = self._sources({"price": "block"}, ctx=other.ctx)
        other_draft = self._batch(other)

        response = self._start(other_draft, other_policy, own_fact_set, client=other.client)

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "truth_configuration_invalid")
        with tenant_session(other.ctx) as session:
            self.assertIsNone(session.get(LocalizationBatchRecord, other_draft["batch_id"]).truth_package_id)
            self.assertEqual(
                session.query(TruthPackageRecord).filter_by(workspace_id=other.ctx.workspace_id).count(), 0,
            )
