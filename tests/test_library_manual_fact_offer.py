"""#54: every manual fact row names its Offer, and publish depends on that Offer.

Contract §11.4 and acceptance items 12 and 13
(docs/product/creative-library-ownership-and-reuse-contract.md): a Manual Fact
row carries its Offer, the row-by-row report checks it, and an Offer or fact
change reaches only what really depends on it.

- A row names the Offer *entry* in ``offer_entry_id``, the way a
  TruthResolutionPolicy names its data sources (#70).
- An import may carry a default Offer: it fills the rows that name none; a
  row that names its own Offer keeps it.
- The report (#23) gives a row whose Offer is missing, not an Offer of this
  Workspace, not published, or does not sell in the row's market a blocking
  finding at ``facts[i].offer_entry_id``. The draft is still created.
- Publish stores one ``fact_set_offer`` edge per distinct OfferVersion the
  rows resolve to, so a withdraw preview of that version lists the fact set,
  and an Offer no row names never does.

Imports go through the HTTP route and the Worker; Offers, data sources and
fact sets publish through the Library commands. No LibraryVersion row is
inserted directly.
"""
from __future__ import annotations

import json

from sqlalchemy import select

from tests.support.offers import offer_content, publish_offer, publish_offers, publish_product, republish_offer
from tests.test_library_manual_fact_row_report import FactRowReportFixtures, _fact

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import LibraryDependencyRecord
from app.library import service
from app.library.contracts import (
    DRAFT_READY,
    DRAFT_VALIDATION_FAILED,
    ENTRY_MANUAL_FACT_SET,
    ENTRY_OFFER,
    ENTRY_PRODUCT,
    ROLE_MEMBER,
    ROLE_OWNER,
)


def _price(fact_id: str, country: str = "US", amount: str = "19.99", **values: object) -> dict:
    return _fact(fact_id, amount=amount, currency="USD", country_code=country, **values)


class OfferFixtures(FactRowReportFixtures):
    def offers(self, *skus: str, markets=("US",), ctx=None) -> list[tuple[str, str]]:
        """Published Offers of one Product: ``[(entry id, version id), ...]``."""
        return publish_offers(ctx or self.ctx, markets=list(markets), skus=skus)

    def import_rows(self, rows: list, *, default_offer: str | None = None):
        """Import ``rows`` through the route and the Worker: ``(fact set id, draft)``."""
        data_source = self.published_data_source()
        upload = self.upload_file(data_source, json.dumps({"facts": rows}).encode(), "json")
        accepted = self.import_(data_source, upload, default_offer=default_offer)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.drain()
        return self.imported_draft(self.operation(accepted.json()["operation_id"]))

    def offer_edges(self, version_id: str) -> set[str]:
        with tenant_session(self.ctx) as session:
            return set(session.scalars(
                select(LibraryDependencyRecord.to_version_id).where(
                    LibraryDependencyRecord.from_version_id == version_id,
                    LibraryDependencyRecord.relation == "fact_set_offer",
                )
            ))

    def withdraw_preview_lists(self, version_id: str) -> set[str]:
        response = self.client.post(f"/api/v1/library/versions/{version_id}/withdraw-impact")
        self.assertEqual(response.status_code, 201, response.text)
        return {item["version_id"] for item in response.json()["projection"]["dependents"]}


class TestTheDefaultOffer(OfferFixtures):
    def test_it_fills_the_rows_that_name_no_offer_and_keeps_the_others(self):
        (offer_a, version_a), (offer_b, version_b) = self.offers("ACME-65W", "ACME-100W")
        rows = [
            _price("no-offer"),
            _price("own-offer", offer_entry_id=offer_b),
            _price("empty-offer", offer_entry_id=""),
            _price("null-offer", offer_entry_id=None),
        ]

        fact_set_id, draft = self.import_rows(rows, default_offer=offer_a)

        self.assertEqual(
            [fact["offer_entry_id"] for fact in draft["content"]["facts"]],
            [offer_a, offer_b, offer_a, offer_a],
        )
        self.assertEqual(draft["state"], DRAFT_READY)
        self.assertEqual(draft["validation"]["blocking"], [])
        published = self.command(fact_set_id, "publish", draft["revision"])
        self.assertEqual(published.status_code, 201, published.text)
        self.assertEqual(self.offer_edges(published.json()["id"]), {version_a, version_b})

    def test_the_job_carries_the_default_offer(self):
        [(offer_a, _version)] = self.offers("ACME-65W")
        data_source = self.published_data_source()
        upload = self.upload_file(data_source, json.dumps({"facts": [_price("p")]}).encode(), "json")
        self.assertEqual(self.import_(data_source, upload, default_offer=offer_a).status_code, 202)
        [job] = self.jobs()
        self.assertEqual(job.payload_json["default_offer_entry_id"], offer_a)

    def test_without_a_default_each_row_must_name_its_offer(self):
        [(offer_a, _version)] = self.offers("ACME-65W")
        fact_set_id, draft = self.import_rows([_price("no-offer"), _price("own", offer_entry_id=offer_a)])

        self.assertEqual(draft["state"], DRAFT_VALIDATION_FAILED)
        self.assertEqual(self.findings(draft["validation"]), [("fact_offer_required", "facts[0].offer_entry_id")])
        blocked = self.command(fact_set_id, "publish", draft["revision"])
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertIsNone(service.get_entry(self.ctx, fact_set_id)["current_version_id"])

    def test_a_default_that_is_not_a_published_active_offer_of_the_workspace_is_refused(self):
        [(foreign_offer, _version)] = self.offers("ACME-65W", ctx=self._make_context(role=ROLE_OWNER))
        _product, product_version = publish_product(self.ctx)
        draft_only = service.create_entry(
            self.ctx, entry_type=ENTRY_OFFER, display_name="Offer draft",
            content=offer_content(product_version, markets=["US"], sku="ACME-DRAFT"),
        )["id"]
        archived, _archived_version = publish_offer(self.ctx, product_version, markets=["US"], sku="ACME-OLD")
        service.archive_entry(
            self.ctx, archived, expected_revision=service.get_entry(self.ctx, archived)["draft"]["revision"],
        )
        product = service.create_entry(self.ctx, entry_type=ENTRY_PRODUCT, display_name="Not an offer")["id"]
        data_source = self.published_data_source()

        for name, default in {
            "another Workspace's Offer": foreign_offer,
            "an unpublished Offer": draft_only,
            "an archived Offer": archived,
            "a Product": product,
            "an id that does not exist": "lib_does_not_exist",
        }.items():
            with self.subTest(name):
                upload = self.upload_file(data_source, json.dumps({"facts": [_price("p")]}).encode(), "json")
                self.assert_problem(self.import_(data_source, upload, default_offer=default), 422, "default_offer_invalid")
        self.assertEqual(self.jobs(), [], "a refused default queues nothing")

    def test_a_key_replayed_with_another_default_is_a_conflict(self):
        (offer_a, _a), (offer_b, _b) = self.offers("ACME-65W", "ACME-100W")
        data_source = self.published_data_source()
        upload = self.upload_file(data_source, json.dumps({"facts": [_price("p")]}).encode(), "json")
        key = "ci-only-factimport-default-offer"
        first = self.import_(data_source, upload, key, default_offer=offer_a)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self.import_(data_source, upload, key, default_offer=offer_a).json(), first.json())
        self.assert_problem(self.import_(data_source, upload, key, default_offer=offer_b), 409, "idempotency_key_reused")
        self.assert_problem(self.import_(data_source, upload, key), 409, "idempotency_key_reused")


class TestEachRowsOffer(OfferFixtures):
    def test_another_workspaces_offer_is_a_row_problem_and_publish_is_refused(self):
        [(foreign_offer, _version)] = self.offers("ACME-65W", ctx=self._make_context(role=ROLE_OWNER))
        [(own_offer, _own)] = self.offers("ACME-65W")

        fact_set_id, draft = self.import_rows([
            _price("own", offer_entry_id=own_offer),
            _price("foreign", offer_entry_id=foreign_offer),
        ])

        self.assertEqual(draft["state"], DRAFT_VALIDATION_FAILED)
        self.assertEqual(self.findings(draft["validation"]), [("fact_offer_not_found", "facts[1].offer_entry_id")])
        blocked = self.command(fact_set_id, "publish", draft["revision"])
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(self.findings(blocked.json()["report"]), [("fact_offer_not_found", "facts[1].offer_entry_id")])
        self.assertIsNone(service.get_entry(self.ctx, fact_set_id)["current_version_id"])

    def test_a_row_outside_its_offers_markets_is_a_row_problem(self):
        [(us_offer, _version)] = self.offers("ACME-65W", markets=("US", "DE"))
        _fact_set_id, draft = self.import_rows([
            _price("us", offer_entry_id=us_offer),
            _price("gb", country="GB", offer_entry_id=us_offer),
            _price("de", country="DE", offer_entry_id=us_offer),
        ])
        self.assertEqual(self.findings(draft["validation"]), [("fact_offer_market_scope", "facts[1].offer_entry_id")])

    def test_an_offer_that_cannot_be_newly_referenced_is_a_row_problem(self):
        _product, product_version = publish_product(self.ctx)
        draft_only = service.create_entry(
            self.ctx, entry_type=ENTRY_OFFER, display_name="Offer draft",
            content=offer_content(product_version, markets=["US"], sku="ACME-DRAFT"),
        )["id"]
        withdrawn, withdrawn_version = publish_offer(self.ctx, product_version, markets=["US"], sku="ACME-RECALL")
        service.withdraw_version(self.ctx, withdrawn_version, reason="Wrong landing page")
        archived, _archived_version = publish_offer(self.ctx, product_version, markets=["US"], sku="ACME-OLD")
        service.archive_entry(
            self.ctx, archived, expected_revision=service.get_entry(self.ctx, archived)["draft"]["revision"],
        )
        product = service.create_entry(self.ctx, entry_type=ENTRY_PRODUCT, display_name="Not an offer")["id"]

        _fact_set_id, draft = self.import_rows([
            _price("draft-only", offer_entry_id=draft_only),
            _price("withdrawn", offer_entry_id=withdrawn),
            _price("archived", offer_entry_id=archived),
            _price("product", offer_entry_id=product),
            _price("unknown", offer_entry_id="lib_does_not_exist"),
        ])

        self.assertEqual(self.findings(draft["validation"]), [
            ("fact_offer_unpublished", "facts[0].offer_entry_id"),
            ("dependency_withdrawn", "facts[1].offer_entry_id"),
            ("dependency_archived", "facts[2].offer_entry_id"),
            ("fact_offer_not_found", "facts[3].offer_entry_id"),
            ("fact_offer_not_found", "facts[4].offer_entry_id"),
        ])

    def test_a_member_imports_and_fixes_rows_but_only_the_owner_publishes(self):
        [(offer_a, _version)] = self.offers("ACME-65W")
        member = self._make_context(role=ROLE_MEMBER, workspace_id=self.ctx.workspace_id, actor="user_m")
        data_source = self.published_data_source()
        self.act_as(member)
        upload = self.upload_file(data_source, json.dumps({"facts": [_price("p")]}).encode(), "json", ctx=member)
        accepted = self.import_(data_source, upload, default_offer=offer_a)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.drain()
        fact_set_id, draft = self.imported_draft(self.operation(accepted.json()["operation_id"]))
        self.assertEqual(draft["validation"]["blocking"], [])

        denied = self.command(fact_set_id, "publish", draft["revision"])
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertIsNone(service.get_entry(self.ctx, fact_set_id)["current_version_id"])

        self.act_as(self.ctx)
        self.assertEqual(self.command(fact_set_id, "publish", draft["revision"]).status_code, 201)

    def test_another_workspace_cannot_publish_the_fact_set(self):
        [(offer_a, _version)] = self.offers("ACME-65W")
        fact_set_id, draft = self.import_rows([_price("p")], default_offer=offer_a)
        self.act_as(self._make_context(role=ROLE_OWNER, actor="user_b"))
        self.assertEqual(self.command(fact_set_id, "publish", draft["revision"]).status_code, 404)
        self.assertIsNone(service.get_entry(self.ctx, fact_set_id)["current_version_id"])


class TestOnlyRealDependencies(OfferFixtures):
    """Acceptance item 12: an Offer change reaches only what really depends on it."""

    def test_the_fact_set_depends_on_the_offers_its_rows_name_and_nothing_else(self):
        (offer_a, a_1), (offer_b, b_1), (_offer_c, c_1) = self.offers("ACME-65W", "ACME-100W", "ACME-CABLE")
        fact_set_id, draft = self.import_rows([
            _price("a-us", offer_entry_id=offer_a),
            _price("b-us", amount="24.99", offer_entry_id=offer_b),
            _price("a-us-later", amount="18.99", offer_entry_id=offer_a, valid_from="2099-01-01"),
        ])
        published = self.command(fact_set_id, "publish", draft["revision"])
        self.assertEqual(published.status_code, 201, published.text)
        fact_set_v1 = published.json()["id"]

        self.assertEqual(self.offer_edges(fact_set_v1), {a_1, b_1}, "one edge per OfferVersion")
        self.assertIn(fact_set_v1, self.withdraw_preview_lists(a_1))
        self.assertIn(fact_set_v1, self.withdraw_preview_lists(b_1))
        self.assertNotIn(fact_set_v1, self.withdraw_preview_lists(c_1), "no row names Offer C")

        # Republishing Offer A supersedes A v1; the published fact set keeps
        # its frozen edge to A v1, and nothing about Offer B changes. The rows
        # name Offer A's entry, so a withdraw preview of either A version
        # lists the fact set (#88).
        a_2 = republish_offer(self.ctx, offer_a, {
            **service.get_version(self.ctx, a_1)["content"],
            "landing_page_url": "https://acme-charge.example/offers/acme-65w-new",
        })
        self.assertEqual(self.offer_edges(fact_set_v1), {a_1, b_1})
        self.assertIn(fact_set_v1, self.withdraw_preview_lists(a_1))
        self.assertIn(fact_set_v1, self.withdraw_preview_lists(a_2))
        self.assertIn(fact_set_v1, self.withdraw_preview_lists(b_1))
        self.assertEqual(service.get_version(self.ctx, fact_set_v1)["state"], "effective")

        # The rows name the entry, so they stay valid for A v2 without a
        # rewrite: the next fact set version depends on A v2.
        entry = service.get_entry(self.ctx, fact_set_id)
        saved = self.save(fact_set_id, entry["draft"]["revision"], {
            **entry["draft"]["content"],
            "facts": [*entry["draft"]["content"]["facts"], _price("b-us-2", amount="23.99", offer_entry_id=offer_b)],
        })
        republished = self.command(fact_set_id, "publish", saved["revision"])
        self.assertEqual(republished.status_code, 201, republished.text)
        self.assertEqual(self.offer_edges(republished.json()["id"]), {a_2, b_1})

    def test_withdrawing_an_offer_version_blocks_the_fact_set_that_names_it(self):
        (offer_a, a_1), (_offer_b, b_1) = self.offers("ACME-65W", "ACME-100W")
        fact_set_id, draft = self.import_rows([_price("a", offer_entry_id=offer_a)])
        fact_set_v1 = self.command(fact_set_id, "publish", draft["revision"]).json()["id"]

        service.withdraw_version(self.ctx, b_1, reason="Discontinued bundle")
        self.assertEqual(service.get_version(self.ctx, fact_set_v1)["eligibility"], "eligible")

        service.withdraw_version(self.ctx, a_1, reason="Wrong price list")
        self.assertEqual(service.get_version(self.ctx, fact_set_v1)["eligibility"], "blocked")


class TestEntryContent(OfferFixtures):
    def test_a_fact_set_saved_by_hand_is_checked_the_same_way(self):
        [(offer_a, _version)] = self.offers("ACME-65W")
        entry = service.create_entry(
            self.ctx, entry_type=ENTRY_MANUAL_FACT_SET, display_name="Facts by hand",
            content={"data_source_entry_id": self.published_data_source(), "facts": [
                _price("ok", offer_entry_id=offer_a),
                _price("missing"),
                _price("wrong-market", country="DE", offer_entry_id=offer_a),
            ]},
        )
        report = self.command(entry["id"], "validate", entry["draft"]["revision"])
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(self.findings(report.json()), [
            ("fact_offer_required", "facts[1].offer_entry_id"),
            ("fact_offer_market_scope", "facts[2].offer_entry_id"),
        ])
