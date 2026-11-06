"""Issue #269 (C-13) -- competitor list, resolve Job and page confirmation, end to end.

Real HTTP routes (``TestClient``) and the real Worker (``WorkerRuntime``)
with the Controlled Mock over the P0 recordings. "byok" runs put a mock in
place of ScrapeCreators (no real key, no real request) so reservations,
settlement by ``credits_charged`` and 402 release are exercised for real.

* Spec scenario 1: an Ad Library link confirms at once; no resolve, no
  ``page_search`` or ``probe`` attempt.
* Scenario 2: "Acme Charge" + ``acme-charge.example`` quotes 1 + 3 credits, finds
  >= 5 candidates, the probe hits ``acmecharge`` and auto-confirms it; with
  no website nothing is confirmed automatically.
* Brightvolt outranks the Czech event page after scoring and the probe.
* Scenario 12: Members create, resolve and confirm; only the Owner
  archives; another Workspace's competitor or resolve is 404.
* Scenario 14: the product's own page joins as ``role=self``.
* Repeated confirmations return the same revision; Idempotency-Key replays.
* A 402 mid-resolve stops it and releases every unused reservation.
* One market-data job per key at a time (§5.1): the second waits.
* Confirmations are append-only.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select, text, update
from sqlalchemy.exc import DatabaseError

from tests.support.queue_hygiene import quarantine_foreign_jobs
from tests.test_test_plans_api import PROTECTOR, PlanFixtures

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence.models import JobRecord, OperationRecord, utcnow
from app.competitors import v1_router
from app.competitors.models import CompetitorIdentityConfirmationRecord, CompetitorResolutionRecord
from app.competitors.sources.mock import MockAdLibrarySource
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendGuardrailRecord,
    SpendReservationRecord,
)
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.models import EntitlementProjectionRecord
from app.library.contracts import ROLE_MEMBER
from app.worker_runtime import WorkerRuntime

ACME = "100000000000010"
BRIGHTVOLT = "100000000000016"
PRICE = {"credits": 1, "credit_usd": "0.00188"}
PRICES = json.dumps({"scrapecreators": {op: PRICE for op in ("page_search", "probe", "page", "keyword_page", "ad_refresh")}})
AD_LIBRARY_LINK = (
    "https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=US"
    f"&view_all_page_id={ACME}"
)


class CompetitorFixtures(PlanFixtures):
    def setUp(self) -> None:
        super().setUp()
        self.mock_credits = 100
        self.sources: list[MockAdLibrarySource] = []
        prices = patch.object(config, "MARKET_DATA_MAX_COSTS_JSON", PRICES)
        prices.start()
        self.addCleanup(prices.stop)
        #: Where the Worker archives avatars and the API signs them (#332).
        self.blob_store = LocalFileBlobStore(
            root=self.blob_root.name, public_base_url="http://blob.test", signing_secret=b"test-secret",
        )
        signer = patch.object(v1_router, "_blob_store", self.blob_store)
        signer.start()
        self.addCleanup(signer.stop)

    # -- helpers ----------------------------------------------------------

    def _source(self, mode: str) -> MockAdLibrarySource:
        source = MockAdLibrarySource(credits=self.mock_credits)
        if mode == "byok":  # the mock stands in for ScrapeCreators; nothing leaves the test
            source.descriptor = dataclasses.replace(source.descriptor, provider="scrapecreators")
        self.sources.append(source)
        return source

    def _drain(self) -> None:
        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner=f"competitors-{uuid.uuid4().hex[:6]}")
        runtime.blob_store = self.blob_store
        if getattr(self, "media_fetcher", None) is not None:  # the free avatar downloads (#332)
            runtime.competitor_media_fetcher = lambda mode: self.media_fetcher
        runtime.ad_library_source = self._source
        runtime._market_data_protector = PROTECTOR
        for _ in range(50):
            if not asyncio.run(runtime.run_once()):
                break

    def _bind_market_data(self) -> None:
        owner = self.owner_ctx(self.ctx)
        with tenant_session(owner) as session:
            session.add(EntitlementProjectionRecord(
                workspace_id=self.ctx.workspace_id,
                entitlement_version=1, lease_sequence=1, projection_version=1,
                semantic_hash="hash_competitors", resolved_entitlements_json={"features": ["generation"]},
            ))
        admin = CredentialAdmin(PROTECTOR)
        with tenant_session(owner) as session:
            meta = admin.create(
                session, owner, credential_key="SCRAPECREATORS_API_KEY", provider="scrapecreators",
                plaintext=b"sc-test-placeholder",
            )
            admin.bind(
                session, owner, binding_scope="market_data", provider="scrapecreators",
                credential_version_id=meta.credential_version_id,
            )
        self.credential_version_id = meta.credential_version_id

    def _create(self, body: dict, *, key: str | None = None, expect: int = 201) -> dict:
        response = self.client.post("/api/v1/competitors", json=body, headers=self._headers(key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _competitor(self, input_text: str, *, website: str | None = None, role: str = "competitor",
                    product_id: str | None = None) -> dict:
        body = {"product_id": product_id or self.product_id, "input": input_text, "role": role}
        if website:
            body["website"] = website
        return self._create(body)

    def _quote(self, competitor_id: str, expect: int = 200, **params) -> dict:
        response = self.client.get(f"/api/v1/competitors/{competitor_id}/resolve-quote", params=params)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _resolve(self, competitor_id: str, body: dict | None = None, *, key: str | None = None,
                 expect: int = 202) -> dict:
        response = self.client.post(
            f"/api/v1/competitors/{competitor_id}/resolve", json=body or {}, headers=self._headers(key),
        )
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _resolution(self, competitor_id: str, job_id: str, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/competitors/{competitor_id}/resolutions/{job_id}")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _confirm(self, competitor_id: str, body: dict, *, key: str | None = None, expect: int = 200) -> dict:
        response = self.client.post(
            f"/api/v1/competitors/{competitor_id}/confirm", json=body, headers=self._headers(key),
        )
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _get(self, competitor_id: str, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/competitors/{competitor_id}")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _problem(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual(body["code"], code, body)
        return body

    def _attempts(self, job_id: str | None = None) -> list[ProviderCallAttemptRecord]:
        with tenant_session(self.ctx) as session:
            query = select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == self.ctx.workspace_id,
                ProviderCallAttemptRecord.step_kind == "market_data",
            )
            if job_id is not None:
                query = query.where(ProviderCallAttemptRecord.job_id == job_id)
            rows = session.scalars(query).all()
            for row in rows:
                session.expunge(row)
            return rows

    def _reservations(self, job_id: str) -> dict[str, str]:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == self.ctx.workspace_id,
                SpendReservationRecord.job_id == job_id,
            )).all()
            return {row.semantic_key_hash: row.state for row in rows}

    @property
    def product_id(self) -> str:
        if getattr(self, "_competitor_product", None) is None:
            self._competitor_product = self._product()
        return self._competitor_product


class CompetitorListTests(CompetitorFixtures):
    def test_scenario_1_an_ad_library_link_confirms_without_a_resolve(self):
        competitor = self._competitor(AD_LIBRARY_LINK)
        self.assertEqual(competitor["input_kind"], "ad_library")
        self.assertEqual(competitor["confirmation_status"], "confirmed")
        confirmation = competitor["confirmation"]
        self.assertEqual(
            (confirmation["revision"], confirmation["page_id"], confirmation["source"], confirmation["link_kind"]),
            (1, ACME, "link", "ad_library"),
        )
        self.assertIsNone(confirmation["resolve_job_id"])
        self.assertIsNone(competitor["latest_resolution"])
        self.assertFalse(competitor["can_resolve"])
        self._drain()
        self.assertEqual(self._attempts(), [])
        self.assertEqual(self.sources, [])
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.scalar(select(text("count(*)")).select_from(CompetitorResolutionRecord)
                                            .where(CompetitorResolutionRecord.workspace_id == self.ctx.workspace_id)), 0)
        problem = self._problem(
            self.client.get(f"/api/v1/competitors/{competitor['competitor_id']}/resolve-quote"),
            409, "competitor_resolve_needs_name",
        )
        self.assertEqual(problem["status"], 409)

    def test_create_replays_by_key_and_refuses_a_reused_key(self):
        key = f"ik-{uuid.uuid4().hex}"
        first = self._create({"product_id": self.product_id, "input": "Acme Charge"}, key=key)
        again = self._create({"product_id": self.product_id, "input": "Acme Charge"}, key=key)
        self.assertEqual(again["competitor_id"], first["competitor_id"])
        self.assertTrue(again["replayed"])
        self.assertFalse(first["replayed"])
        response = self.client.post(
            "/api/v1/competitors", json={"product_id": self.product_id, "input": "Halcyon Motors"},
            headers=self._headers(key),
        )
        self._problem(response, 409, "idempotency_key_reused")

    def test_inputs_are_classified_and_refused_with_codes(self):
        refusals = (
            ({"input": "acme-charge.example"}, 422, "competitor_name_required"),
            ({"input": "https://www.facebook.com/groups/1234567"}, 422, "competitor_link_unrecognized"),
            ({"input": "Acme Charge", "website": "facebook.com/acmecharge"}, 422, "competitor_website_invalid"),
            ({"input": "Acme Charge", "role": "rival"}, 422, None),
        )
        for body, status, code in refusals:
            response = self.client.post(
                "/api/v1/competitors", json={"product_id": self.product_id, **body}, headers=self._headers(),
            )
            if code is None:
                self.assertEqual(response.status_code, status, response.text)
            else:
                self._problem(response, status, code)
        problem = self._problem(
            self.client.post("/api/v1/competitors", json={"product_id": self.product_id, "input": "acme-charge.example"},
                             headers=self._headers()),
            422, "competitor_name_required",
        )
        self.assertEqual(problem["params"], {"website_domain": "acme-charge.example"})
        self._problem(
            self.client.post("/api/v1/competitors", json={"product_id": "prod_missing", "input": "Acme Charge"},
                             headers=self._headers()),
            404, "product_not_found",
        )
        alias = self._competitor("https://www.facebook.com/acmecharge", website="https://acme-charge.example/")
        self.assertEqual((alias["input_kind"], alias["input_page_alias"]), ("facebook_page", "acmecharge"))
        self.assertEqual((alias["confirmation_status"], alias["can_resolve"]), ("unconfirmed", True))
        self.assertEqual(alias["website_domain"], "acme-charge.example")

    def test_scenario_14_the_products_own_page_joins_as_self(self):
        own = self._competitor(f"https://www.facebook.com/profile.php?id={BRIGHTVOLT}", role="self")
        rival = self._competitor("Acme Charge")
        self.assertEqual((own["role"], own["confirmation"]["page_id"]), ("self", BRIGHTVOLT))
        listed = self.client.get("/api/v1/competitors", params={"product_id": self.product_id}).json()["items"]
        self.assertEqual({item["competitor_id"]: item["role"] for item in listed},
                         {own["competitor_id"]: "self", rival["competitor_id"]: "competitor"})

    def test_scenario_12_members_create_resolve_and_confirm_only_the_owner_archives(self):
        competitor = self._competitor("Acme Charge", website="acme-charge.example")
        competitor_id = competitor["competitor_id"]
        accepted = self._resolve(competitor_id)
        self._drain()
        resolution = self._resolution(competitor_id, accepted["resolve_job_id"])
        second = resolution["candidates"][1]["page_id"]
        confirmed = self._confirm(competitor_id, {"resolve_job_id": accepted["resolve_job_id"], "page_id": second})
        self.assertEqual((confirmed["confirmation"]["revision"], confirmed["confirmation"]["source"]), (2, "user"))

        response = self.client.post(f"/api/v1/competitors/{competitor_id}/archive", headers=self._headers())
        self._problem(response, 403, "competitor_archive_forbidden")
        self.assertEqual(self._get(competitor_id)["status"], "active")

        self.current = self.owner_ctx(self.ctx)
        archived = self.client.post(f"/api/v1/competitors/{competitor_id}/archive", headers=self._headers())
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertEqual((archived.json()["status"], archived.json()["archived_by"]), ("archived", "owner_a"))
        again = self.client.post(f"/api/v1/competitors/{competitor_id}/archive", headers=self._headers())
        self.assertEqual(again.json()["archived_at"], archived.json()["archived_at"])
        self._problem(self.client.post(f"/api/v1/competitors/{competitor_id}/resolve", json={},
                                       headers=self._headers()), 409, "competitor_archived")
        self._problem(self.client.post(f"/api/v1/competitors/{competitor_id}/confirm",
                                       json={"resolve_job_id": accepted["resolve_job_id"], "page_id": ACME},
                                       headers=self._headers()), 409, "competitor_archived")
        self.assertEqual(self.client.get("/api/v1/competitors", params={"product_id": self.product_id}).json()["items"], [])
        everything = self.client.get("/api/v1/competitors", params={"product_id": self.product_id, "status": "all"})
        self.assertEqual([item["competitor_id"] for item in everything.json()["items"]], [competitor_id])

    def test_scenario_12_another_workspace_sees_nothing(self):
        competitor = self._competitor("Acme Charge", website="acme-charge.example")
        competitor_id = competitor["competitor_id"]
        accepted = self._resolve(competitor_id)
        self._drain()
        other = self._make_context(role=ROLE_MEMBER, actor="member_b")
        self.current = other
        for response in (
            self.client.get(f"/api/v1/competitors/{competitor_id}"),
            self.client.get(f"/api/v1/competitors/{competitor_id}/resolve-quote"),
            self.client.get(f"/api/v1/competitors/{competitor_id}/resolutions/{accepted['resolve_job_id']}"),
            self.client.post(f"/api/v1/competitors/{competitor_id}/resolve", json={}, headers=self._headers()),
            self.client.post(f"/api/v1/competitors/{competitor_id}/confirm",
                             json={"page_url": AD_LIBRARY_LINK}, headers=self._headers()),
        ):
            self._problem(response, 404, "competitor_not_found")
        self.current = self.owner_ctx(other)
        self._problem(self.client.post(f"/api/v1/competitors/{competitor_id}/archive", headers=self._headers()),
                      404, "competitor_not_found")
        self._problem(
            self.client.post("/api/v1/competitors", json={"product_id": self.product_id, "input": "Acme Charge"},
                             headers=self._headers()),
            404, "product_not_found",
        )
        self.assertEqual(self.client.get("/api/v1/competitors").json()["items"], [])
        self.current = self.ctx
        self._problem(
            self.client.get(f"/api/v1/competitors/{competitor_id}/resolutions/job_missing"),
            404, "competitor_resolution_not_found",
        )


class CompetitorResolveTests(CompetitorFixtures):
    def test_scenario_2_acme_with_its_site_resolves_and_confirms_itself(self):
        competitor = self._competitor("Acme Charge", website="acme-charge.example")
        competitor_id = competitor["competitor_id"]
        quote = self._quote(competitor_id)
        self.assertEqual((quote["execution_mode"], quote["available_modes"]), ("mock", ["mock"]))
        self.assertEqual(quote["byok_unavailable_code"], "market_data_credential_missing")
        self.assertEqual(quote["max_credits"], 4)
        self.assertEqual([(line["operation"], line["calls"], line["credits"]) for line in quote["lines"]],
                         [("page_search", 1, 1), ("probe", 3, 3)])
        self.assertEqual((quote["max_usd"], quote["simulated_usd"]), ("0", "0.00752"))
        self._problem(self.client.get(f"/api/v1/competitors/{competitor_id}/resolve-quote",
                                      params={"execution_mode": "byok"}), 409, "market_data_credential_missing")
        self._problem(self.client.get(f"/api/v1/competitors/{competitor_id}/resolve-quote",
                                      params={"country": "ZZ"}), 422, "competitor_country_invalid")

        accepted = self._resolve(competitor_id)
        self.assertEqual((accepted["status"], accepted["execution_mode"], accepted["provider"]),
                         ("queued", "mock", "mock"))
        self.assertEqual(accepted["quote"]["max_credits"], 4)
        self._drain()
        resolution = self._resolution(competitor_id, accepted["resolve_job_id"])
        self.assertEqual(resolution["status"], "completed", resolution)
        self.assertGreaterEqual(len(resolution["candidates"]), 5)
        top = resolution["candidates"][0]
        self.assertEqual((top["page_id"], top["page_alias"], top["name_match"], top["rank"]),
                         (ACME, "acmecharge", "exact", 0))
        self.assertTrue(top["domain_hit"])
        self.assertEqual(top["probe"]["landing_domains"], ["acme-charge.example"])
        # The Mock's placeholder avatar, archived by the resolve and signed (#332).
        self.assertEqual(top["avatar"]["mime_type"], "image/jpeg")
        self.assertTrue(top["avatar"]["url"].startswith("http://blob.test/"))
        self.assertNotIn("avatar_url", top)
        self.assertTrue(resolution["auto_confirmed"])
        self.assertEqual(resolution["confirmation_revision"], 1)
        self.assertEqual(resolution["cost"], {"calls": 4, "credits_charged": 4, "usd": "0",
                                              "simulated_usd": "0.00752"})
        self.assertTrue(resolution["reusable"])
        self.assertIsNotNone(resolution["expires_at"])
        competitor = self._get(competitor_id)
        self.assertEqual(competitor["confirmation_status"], "confirmed")
        confirmation = competitor["confirmation"]
        self.assertEqual((confirmation["source"], confirmation["page_id"], confirmation["resolve_job_id"]),
                         ("auto", ACME, accepted["resolve_job_id"]))
        self.assertTrue(confirmation["domain_hit"])
        self.assertIsNone(confirmation["candidate"]["avatar"])
        self.assertEqual(competitor["latest_resolution"]["resolve_job_id"], accepted["resolve_job_id"])

        attempts = self._attempts(accepted["resolve_job_id"])
        self.assertEqual(sorted(attempt.model for attempt in attempts), ["page_search", "probe", "probe", "probe"])
        self.assertEqual({(a.execution_mode, a.chargeability, a.dispatch_state) for a in attempts},
                         {("mock", "free_mock", "succeeded")})
        with tenant_session(self.ctx) as session:
            operation = session.scalars(select(OperationRecord).where(
                OperationRecord.id == accepted["operation_id"])).one()
            self.assertEqual(operation.state, "succeeded")
        self.assert_stored_widths()

    def test_346_a_quote_line_carries_its_total_credits(self):
        competitor = self._competitor("Acme Charge", website="acme-charge.example")["competitor_id"]
        two = {"scrapecreators": {op: PRICE | {"credits": 2}
                                  for op in ("page_search", "probe", "page", "keyword_page", "ad_refresh")}}
        with patch.object(config, "MARKET_DATA_MAX_COSTS_JSON", json.dumps(two)):
            quote = self._quote(competitor)
        lines = [(line["operation"], line["calls"], line["credits"]) for line in quote["lines"]]
        self.assertEqual(lines, [("page_search", 1, 2), ("probe", 3, 6)])
        self.assertEqual(quote["max_credits"], sum(credits for _, _, credits in lines))

    def test_scenario_2_without_a_site_nothing_is_confirmed_automatically(self):
        competitor = self._competitor("Acme Charge")
        accepted = self._resolve(competitor["competitor_id"])
        self._drain()
        resolution = self._resolution(competitor["competitor_id"], accepted["resolve_job_id"])
        self.assertEqual(resolution["status"], "completed")
        self.assertEqual(resolution["candidates"][0]["page_id"], ACME)
        self.assertIsNone(resolution["candidates"][0]["domain_hit"])
        self.assertFalse(resolution["auto_confirmed"])
        self.assertEqual(self._get(competitor["competitor_id"])["confirmation_status"], "unconfirmed")

    def test_brightvolt_outranks_the_czech_event_page(self):
        competitor = self._competitor("Brightvolt", website="https://brightvolt.example")
        accepted = self._resolve(competitor["competitor_id"])
        self._drain()
        resolution = self._resolution(competitor["competitor_id"], accepted["resolve_job_id"])
        candidates = resolution["candidates"]
        self.assertEqual((candidates[0]["page_id"], candidates[0]["provider_rank"]), (BRIGHTVOLT, 4))
        self.assertTrue(candidates[0]["domain_hit"])
        event = next(item for item in candidates if item["provider_rank"] == 0)
        self.assertEqual((event["category"], event["excluded_reason"]), ("Event", "category"))
        self.assertTrue(resolution["auto_confirmed"])
        self.assertEqual(self._get(competitor["competitor_id"])["confirmation"]["page_id"], BRIGHTVOLT)

    def test_a_pasted_alias_is_resolved_by_its_alias(self):
        competitor = self._competitor("https://www.facebook.com/acmecharge")
        quote = self._quote(competitor["competitor_id"])
        self.assertEqual(quote["query"], "acmecharge")
        accepted = self._resolve(competitor["competitor_id"])
        # The recording is keyed by "Acme Charge"; let the alias query read it.
        from app.competitors.sources import mock as mock_module

        fixtures = mock_module.load_fixtures()
        key = next(k for k in fixtures.recorded if dict(k[1]).get("query") == "acme charge")
        with patch.dict(fixtures.recorded, {(key[0], (("query", "acmecharge"),)): fixtures.recorded[key]}):
            self._drain()
        resolution = self._resolution(competitor["competitor_id"], accepted["resolve_job_id"])
        self.assertTrue(resolution["candidates"][0]["alias_match"])
        self.assertTrue(resolution["auto_confirmed"])
        confirmation = self._get(competitor["competitor_id"])["confirmation"]
        self.assertEqual((confirmation["page_id"], confirmation["page_alias"]), (ACME, "acmecharge"))

    def test_confirming_the_same_page_twice_returns_the_same_revision(self):
        competitor = self._competitor("Acme Charge")
        competitor_id = competitor["competitor_id"]
        accepted = self._resolve(competitor_id)
        self._drain()
        body = {"resolve_job_id": accepted["resolve_job_id"], "page_id": ACME}
        key = f"ik-{uuid.uuid4().hex}"
        first = self._confirm(competitor_id, body, key=key)
        self.assertEqual((first["confirmation"]["revision"], first["replayed"]), (1, False))
        by_key = self._confirm(competitor_id, body, key=key)
        by_content = self._confirm(competitor_id, body)
        self.assertEqual((by_key["confirmation"]["revision"], by_key["replayed"]), (1, True))
        self.assertEqual((by_content["confirmation"]["revision"], by_content["replayed"]), (1, True))
        self._problem(self.client.post(f"/api/v1/competitors/{competitor_id}/confirm",
                                       json={"page_url": AD_LIBRARY_LINK}, headers=self._headers(key)),
                      409, "idempotency_key_reused")
        # Re-confirming to another page appends; the old revision stays.
        relinked = self._confirm(competitor_id, {"page_url": f"https://facebook.com/profile.php?id={BRIGHTVOLT}"})
        self.assertEqual((relinked["confirmation"]["revision"], relinked["confirmation"]["source"]), (2, "link"))
        with tenant_session(self.ctx) as session:
            revisions = session.scalars(select(CompetitorIdentityConfirmationRecord.page_id).where(
                CompetitorIdentityConfirmationRecord.competitor_id == competitor_id,
            ).order_by(CompetitorIdentityConfirmationRecord.revision)).all()
        self.assertEqual(revisions, [ACME, BRIGHTVOLT])

    def test_confirmations_are_refused_when_the_target_is_wrong(self):
        competitor = self._competitor("Acme Charge")
        competitor_id = competitor["competitor_id"]
        accepted = self._resolve(competitor_id)
        job_id = accepted["resolve_job_id"]
        self._problem(self.client.post(f"/api/v1/competitors/{competitor_id}/confirm",
                                       json={"resolve_job_id": job_id, "page_id": ACME},
                                       headers=self._headers()), 409, "competitor_resolution_not_ready")
        self._drain()
        for body, status, code in (
            ({"resolve_job_id": job_id}, 422, "competitor_confirm_target_required"),
            ({"resolve_job_id": job_id, "page_id": ACME, "page_url": AD_LIBRARY_LINK}, 422,
             "competitor_confirm_target_required"),
            ({"resolve_job_id": job_id, "page_id": "999"}, 422, "competitor_candidate_not_found"),
            ({"page_url": "https://www.facebook.com/acmecharge"}, 422, "competitor_link_needs_page_id"),
            ({"resolve_job_id": "job_unknown", "page_id": ACME}, 404, "competitor_resolution_not_found"),
        ):
            self._problem(self.client.post(f"/api/v1/competitors/{competitor_id}/confirm", json=body,
                                           headers=self._headers()), status, code)
        with tenant_session(self.ctx) as session:
            session.execute(update(CompetitorResolutionRecord).where(
                CompetitorResolutionRecord.job_id == job_id,
            ).values(expires_at=utcnow() - timedelta(minutes=1)))
        problem = self._problem(self.client.post(f"/api/v1/competitors/{competitor_id}/confirm",
                                                 json={"resolve_job_id": job_id, "page_id": ACME},
                                                 headers=self._headers()), 409, "competitor_resolution_expired")
        self.assertEqual(problem["params"], {"hours": 72})
        self.assertFalse(self._resolution(competitor_id, job_id)["reusable"])

    def test_resolve_replays_by_key(self):
        competitor = self._competitor("Acme Charge")
        key = f"ik-{uuid.uuid4().hex}"
        first = self._resolve(competitor["competitor_id"], key=key)
        again = self._resolve(competitor["competitor_id"], key=key)
        self.assertEqual((again["resolve_job_id"], again["replayed"]), (first["resolve_job_id"], True))
        self._problem(self.client.post(f"/api/v1/competitors/{competitor['competitor_id']}/resolve",
                                       json={"country": "CA"}, headers=self._headers(key)),
                      409, "idempotency_key_reused")

    def test_confirmations_are_append_only(self):
        competitor = self._competitor(AD_LIBRARY_LINK)
        for statement in (
            update(CompetitorIdentityConfirmationRecord)
            .where(CompetitorIdentityConfirmationRecord.competitor_id == competitor["competitor_id"])
            .values(page_id="1"),
            CompetitorIdentityConfirmationRecord.__table__.delete().where(
                CompetitorIdentityConfirmationRecord.competitor_id == competitor["competitor_id"]),
        ):
            with self.assertRaises(DatabaseError):
                with tenant_session(self.ctx) as session:
                    session.execute(statement)


class CompetitorByokTests(CompetitorFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_market_data()

    def test_byok_reserves_each_call_and_settles_by_credits_charged(self):
        competitor = self._competitor("Acme Charge", website="acme-charge.example")
        quote = self._quote(competitor["competitor_id"])
        self.assertEqual((quote["execution_mode"], quote["available_modes"]), ("byok", ["byok", "mock"]))
        self.assertEqual((quote["provider"], quote["max_credits"], quote["max_usd"]), ("scrapecreators", 4, "0.00752"))
        self.assertIsNone(quote["simulated_usd"])
        accepted = self._resolve(competitor["competitor_id"])
        job_id = accepted["resolve_job_id"]
        self.assertEqual(set(self._reservations(job_id).values()), {"reserved"})
        self.assertEqual(len(self._reservations(job_id)), 4)
        self._drain()
        resolution = self._resolution(competitor["competitor_id"], job_id)
        self.assertEqual(resolution["status"], "completed", resolution)
        self.assertEqual(resolution["cost"], {"calls": 4, "credits_charged": 4, "usd": "0.00752",
                                              "simulated_usd": None})
        self.assertEqual(set(self._reservations(job_id).values()), {"settled"})
        attempts = self._attempts(job_id)
        self.assertEqual({(a.execution_mode, a.chargeability, a.credential_version_id) for a in attempts},
                         {("byok", "charged", self.credential_version_id)})
        with tenant_session(self.owner_ctx(self.ctx)) as session:
            reported = session.scalars(select(ProviderCostLedgerEntryRecord.provider_amount).where(
                ProviderCostLedgerEntryRecord.job_id == job_id,
                ProviderCostLedgerEntryRecord.entry_type == "reported_cost",
            )).all()
        self.assertEqual(sorted(Decimal(str(amount)) for amount in reported), [Decimal("0.00188")] * 4)
        self.assertTrue(resolution["auto_confirmed"])

    def test_a_402_mid_resolve_stops_it_and_releases_what_is_left(self):
        self.mock_credits = 2  # the search and one probe; the second probe gets a 402
        competitor = self._competitor("Acme Charge", website="acme-charge.example")
        accepted = self._resolve(competitor["competitor_id"])
        job_id = accepted["resolve_job_id"]
        self._drain()
        resolution = self._resolution(competitor["competitor_id"], job_id)
        self.assertEqual(resolution["status"], "failed", resolution)
        self.assertEqual(resolution["failure"], {"code": "market_data_insufficient_credits", "retryable": False,
                                                 "params": {"provider_code": "insufficient_credits"}})
        self.assertGreaterEqual(len(resolution["candidates"]), 5)  # what was found stays listed
        self.assertEqual(resolution["cost"]["credits_charged"], 2)
        states = sorted(self._reservations(job_id).values())
        self.assertEqual(states, ["released", "released", "settled", "settled"])
        self.assertEqual(len(self._attempts(job_id)), 3)  # probe:3 was never sent
        self.assertFalse(resolution["auto_confirmed"])
        with tenant_session(self.ctx) as session:
            job = session.get(JobRecord, job_id)
            self.assertEqual((job.state, job.error_code), ("failed", "market_data_insufficient_credits"))
        # Candidates of a stopped resolve can still be confirmed by hand.
        confirmed = self._confirm(competitor["competitor_id"], {"resolve_job_id": job_id, "page_id": ACME})
        self.assertEqual(confirmed["confirmation"]["source"], "user")

    def test_a_rejected_key_fails_the_search_and_releases_everything(self):
        competitor = self._competitor("Acme Charge")
        accepted = self._resolve(competitor["competitor_id"])
        original = self._source

        def rejecting(mode):
            source = original(mode)
            source.fail_next(401)
            return source

        self._source = rejecting
        self._drain()
        resolution = self._resolution(competitor["competitor_id"], accepted["resolve_job_id"])
        self.assertEqual((resolution["status"], resolution["failure"]["code"]),
                         ("failed", "market_data_credential_rejected"))
        self.assertEqual(resolution["candidates"], [])
        self.assertFalse(resolution["reusable"])
        self.assertEqual(set(self._reservations(accepted["resolve_job_id"]).values()), {"released"})

    def test_the_guardrail_refuses_the_whole_resolve(self):
        competitor = self._competitor("Acme Charge")
        with tenant_session(self.owner_ctx(self.ctx)) as session:
            from app.generation.spend import ensure_spend_guardrail
            guardrail = ensure_spend_guardrail(session, self.owner_ctx(self.ctx))
            guardrail.per_operation_limit = Decimal("0.005")
        response = self.client.post(f"/api/v1/competitors/{competitor['competitor_id']}/resolve", json={},
                                    headers=self._headers())
        self._problem(response, 409, "guardrail_exceeded")
        with tenant_session(self.ctx) as session:
            self.assertIsNone(session.scalars(select(CompetitorResolutionRecord.id).where(
                CompetitorResolutionRecord.competitor_id == competitor["competitor_id"])).first())
            guardrail = session.get(SpendGuardrailRecord, self.ctx.workspace_id)
            self.assertEqual(guardrail.per_operation_limit, Decimal("0.005"))

    def test_mock_stays_available_and_never_mixes_with_byok(self):
        competitor = self._competitor("Acme Charge")
        accepted = self._resolve(competitor["competitor_id"], {"execution_mode": "mock"})
        self.assertEqual((accepted["execution_mode"], accepted["provider"]), ("mock", "mock"))
        self.assertEqual(self._reservations(accepted["resolve_job_id"]), {})
        self._drain()
        attempts = self._attempts(accepted["resolve_job_id"])
        self.assertEqual({(a.execution_mode, a.credential_version_id) for a in attempts}, {("mock", None)})

    def test_one_market_data_job_per_key_at_a_time(self):
        first = self._resolve(self._competitor("Acme Charge")["competitor_id"])
        second = self._resolve(self._competitor("Brightvolt")["competitor_id"])
        # The first resolve holds the key's turn: running, its Job leased.
        with tenant_session(self.ctx) as session:
            session.execute(update(CompetitorResolutionRecord).where(
                CompetitorResolutionRecord.job_id == first["resolve_job_id"]).values(status="running"))
            session.execute(update(JobRecord).where(JobRecord.id == first["resolve_job_id"]).values(
                state="running", lease_owner="other-worker", lease_expires_at=utcnow() + timedelta(minutes=5)))
        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner="competitors-turn")
        runtime.ad_library_source = self._source
        runtime._market_data_protector = PROTECTOR
        self.assertTrue(asyncio.run(runtime.run_once()))
        waiting = self._resolution(self._get_competitor_of(second), second["resolve_job_id"])
        self.assertEqual((waiting["status"], waiting["waits"]), ("queued", 1))
        self.assertEqual(self._attempts(second["resolve_job_id"]), [])
        with tenant_session(self.ctx) as session:
            job = session.get(JobRecord, second["resolve_job_id"])
            self.assertEqual((job.state, job.error_code), ("retry_wait", "market_data_turn_busy"))
            # The holder's lease lapses: the waiting resolve takes its turn.
            session.execute(update(JobRecord).where(JobRecord.id == first["resolve_job_id"]).values(
                state="cancelled", lease_owner=None, lease_expires_at=None))
            session.execute(update(JobRecord).where(JobRecord.id == second["resolve_job_id"]).values(
                next_attempt_at=utcnow() - timedelta(seconds=1)))
        self._drain()
        done = self._resolution(self._get_competitor_of(second), second["resolve_job_id"])
        self.assertEqual((done["status"], done["waits"]), ("completed", 1))

    def _get_competitor_of(self, resolution: dict) -> str:
        return resolution["competitor_id"]

    def test_318_a_price_table_missing_an_operation_names_it(self):
        competitor = self._competitor("Acme Charge")["competitor_id"]
        partial = {"scrapecreators": {op: PRICE for op in ("page_search", "probe", "page", "keyword_page")}}
        with patch.object(config, "MARKET_DATA_MAX_COSTS_JSON", json.dumps(partial)):
            quote = self._quote(competitor)
            self.assertEqual((quote["execution_mode"], quote["byok_unavailable_code"]),
                             ("mock", "market_data_price_operation_missing"))
            self.assertEqual(quote["byok_unavailable_params"], {"provider": "scrapecreators", "operations": "ad_refresh"})
            for response in (
                self.client.get(f"/api/v1/competitors/{competitor}/resolve-quote", params={"execution_mode": "byok"}),
                self.client.post(f"/api/v1/competitors/{competitor}/resolve", json={"execution_mode": "byok"},
                                 headers=self._headers()),
            ):
                body = self._problem(response, 409, "market_data_price_operation_missing")
                self.assertEqual(body["params"], {"provider": "scrapecreators", "operations": "ad_refresh"})
        # The other two price codes keep their provider param.
        with patch.object(config, "MARKET_DATA_MAX_COSTS_JSON", "{}"):
            quote = self._quote(competitor)
            self.assertEqual((quote["byok_unavailable_code"], quote["byok_unavailable_params"]),
                             ("market_data_prices_unconfigured", {"provider": "scrapecreators"}))
            body = self._problem(self.client.post(f"/api/v1/competitors/{competitor}/resolve",
                                                  json={"execution_mode": "byok"}, headers=self._headers()),
                                 409, "market_data_prices_unconfigured")
            self.assertEqual(body["params"], {"provider": "scrapecreators"})
        with patch.object(config, "MARKET_DATA_MAX_COSTS_JSON", "not json"):
            self.assertEqual(self._quote(competitor)["byok_unavailable_params"], {"provider": "scrapecreators"})


if __name__ == "__main__":
    import unittest

    unittest.main()
