"""Market-data credentials (#267): the ``market_data`` scope, the ScrapeCreators
key check, and the price table every real resolve or scan is quoted from.

No test reaches ScrapeCreators: every credit-balance request goes to an
``httpx.MockTransport`` that records what was sent, and the keys are fixture
strings that exist nowhere else.
"""
from __future__ import annotations

import base64
import json
import unittest
from decimal import Decimal
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AuditEventRecord
from app.main import app
from app.market_data import scrapecreators_account
from app.market_data.credentials import (
    CHECK_EVENT,
    MARKET_DATA_BINDING_SCOPE,
    binding_scope_mismatch,
    resolve_market_data_credential,
)
from app.market_data.pricing import (
    MARKET_DATA_OPERATIONS,
    MarketDataPricingError,
    price_table,
    quote_market_data,
)
from app.market_data.scrapecreators_account import check_credit_balance
from app.platform.http_identity import request_tenant_context
from tests.support.library_fixtures import LibraryFixtures

FIXTURE_KEY = b"fixture-scrapecreators-key-only"
KEY_NAME = "scrapecreators_api_key"

FULL_TABLE = {
    "scrapecreators": {
        op: {"credits": 1, "credit_usd": "0.00188"} for op in MARKET_DATA_OPERATIONS
    }
}


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


class _Vendor:
    """A stand-in for ScrapeCreators' credit-balance endpoint."""

    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    def patch(self):
        original = httpx.AsyncClient
        return patch.object(
            scrapecreators_account.httpx, "AsyncClient",
            side_effect=lambda **kw: original(transport=httpx.MockTransport(self), **kw),
        )


def _balance(credits: int) -> dict:
    return {"success": True, "creditCount": credits, "message": "fixture"}


class PriceTableTests(unittest.TestCase):
    def _with(self, table) -> patch:
        raw = table if isinstance(table, str) else json.dumps(table)
        return patch.object(config, "MARKET_DATA_MAX_COSTS_JSON", raw)

    def _refusal(self, table, provider: str = "scrapecreators") -> MarketDataPricingError:
        with self._with(table), self.assertRaises(MarketDataPricingError) as caught:
            quote_market_data(provider, {"page": 1})
        return caught.exception

    def test_a_complete_table_quotes_credits_usd_and_the_price_version(self):
        with self._with(FULL_TABLE), patch.object(config, "MARKETAI_PRICE_REGISTRY_VERSION", "md-2026-10-28"):
            quote = quote_market_data("scrapecreators", {"page": 9, "keyword_page": 4, "probe": 0})
        self.assertEqual(quote.credits, 13)
        self.assertEqual(quote.usd, Decimal("0.02444"))
        self.assertEqual(quote.pricing_version, "md-2026-10-28")
        self.assertEqual(quote.currency, "USD")
        # Operations with no calls are not quoted lines.
        self.assertEqual([line.operation for line in quote.lines], ["page", "keyword_page"])
        self.assertEqual(quote.to_dict()["lines"][0], {
            "operation": "page", "calls": 9, "credits": 9, "usd": "0.01692",
        })

    def test_multi_credit_operations_multiply(self):
        table = json.loads(json.dumps(FULL_TABLE))
        table["scrapecreators"]["ad_refresh"] = {"credits": 3, "credit_usd": "0.002"}
        with self._with(table):
            quote = quote_market_data("scrapecreators", {"ad_refresh": 2})
        self.assertEqual((quote.credits, quote.usd), (6, Decimal("0.012")))

    def test_no_table_is_unconfigured(self):
        for table in ({}, "", {"other_vendor": FULL_TABLE["scrapecreators"]}, {"scrapecreators": {}}):
            with self.subTest(table=table):
                refusal = self._refusal(table)
                self.assertEqual(refusal.code, "market_data_prices_unconfigured")
                self.assertEqual(refusal.params, {"provider": "scrapecreators"})

    def test_any_missing_operation_refuses_every_quote(self):
        table = json.loads(json.dumps(FULL_TABLE))
        del table["scrapecreators"]["probe"]
        del table["scrapecreators"]["ad_refresh"]
        # ``page`` is priced, but the table is incomplete, so even a page
        # quote is refused.
        refusal = self._refusal(table)
        self.assertEqual(refusal.code, "market_data_price_operation_missing")
        self.assertEqual(refusal.params, {"provider": "scrapecreators", "operations": "probe, ad_refresh"})

    def test_malformed_prices_are_invalid(self):
        cases = {
            "not json": "{nope",
            "list": [],
            "provider not an object": {"scrapecreators": "1"},
            "zero credits": {"credits": 0, "credit_usd": "0.00188"},
            "bool credits": {"credits": True, "credit_usd": "0.00188"},
            "float usd": {"credits": 1, "credit_usd": 0.00188},
            "negative usd": {"credits": 1, "credit_usd": "-0.1"},
            "nan usd": {"credits": 1, "credit_usd": "NaN"},
            "text usd": {"credits": 1, "credit_usd": "cheap"},
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                if isinstance(value, dict) and "credits" in value:
                    table = json.loads(json.dumps(FULL_TABLE))
                    table["scrapecreators"]["page"] = value
                else:
                    table = value
                refusal = self._refusal(table)
                self.assertEqual(refusal.code, "market_data_prices_invalid")
                self.assertEqual(refusal.params, {"provider": "scrapecreators"})

    def test_unknown_operations_and_bad_counts_are_caller_bugs(self):
        with self._with(FULL_TABLE):
            with self.assertRaises(ValueError):
                quote_market_data("scrapecreators", {"video_download": 1})
            for count in (-1, 1.5, True):
                with self.subTest(count=count), self.assertRaises(ValueError):
                    quote_market_data("scrapecreators", {"page": count})

    def test_price_table_returns_all_five_operations(self):
        with self._with(FULL_TABLE):
            table = price_table("scrapecreators")
        self.assertEqual(tuple(table), MARKET_DATA_OPERATIONS)
        self.assertEqual(table["page"].unit_usd, Decimal("0.00188"))


class CreditBalanceCheckTests(unittest.IsolatedAsyncioTestCase):
    async def _check(self, vendor: _Vendor, key: bytes = FIXTURE_KEY):
        with vendor.patch(), patch.object(config, "SCRAPECREATORS_BASE_URL", "https://scrapecreators.test"):
            return await check_credit_balance(key)

    async def test_a_positive_balance_is_valid_and_sends_the_key_in_the_header_only(self):
        vendor = _Vendor(_balance(24987))
        check = await self._check(vendor)
        self.assertEqual((check.outcome, check.credit_count, check.http_status), ("valid", 24987, 200))
        (request,) = vendor.requests
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), "https://scrapecreators.test/v1/account/credit-balance")
        self.assertEqual(request.headers["x-api-key"], FIXTURE_KEY.decode())
        self.assertNotIn(FIXTURE_KEY.decode(), str(request.url))
        self.assertNotIn(FIXTURE_KEY.decode(), repr(check))

    async def test_zero_credits_is_invalid_or_no_balance(self):
        # ScrapeCreators answers 200 with creditCount 0 for an unknown key too.
        check = await self._check(_Vendor(_balance(0)))
        self.assertEqual((check.outcome, check.credit_count), ("invalid_or_no_balance", 0))
        self.assertFalse(check.valid)

    async def test_auth_refusals_are_invalid(self):
        for status in (401, 403):
            with self.subTest(status=status):
                check = await self._check(_Vendor(httpx.Response(status, json={"message": "no"})))
                self.assertEqual(check.outcome, "invalid_or_no_balance")

    async def test_anything_unreadable_is_unavailable_not_invalid(self):
        cases = {
            "429": httpx.Response(429, json={"message": "slow down"}),
            "500": httpx.Response(500, text="oops"),
            "not json": httpx.Response(200, text="<html>"),
            "no count": httpx.Response(200, json={"success": True}),
            "text count": httpx.Response(200, json={"creditCount": "12"}),
            "timeout": httpx.ReadTimeout("slow"),
            "network": httpx.ConnectError("down"),
        }
        for name, reply in cases.items():
            with self.subTest(case=name):
                check = await self._check(_Vendor(reply))
                self.assertEqual(check.outcome, "unavailable")
                self.assertIsNone(check.credit_count)

    async def test_a_key_a_header_cannot_carry_is_refused_without_a_request(self):
        for key in (b"two words", b"line\nbreak", "密钥".encode(), b"   "):
            with self.subTest(key=key):
                vendor = _Vendor(_balance(10))
                check = await self._check(vendor, key)
                self.assertEqual(check.outcome, "invalid_or_no_balance")
                self.assertEqual(vendor.requests, [])


class BindingScopeRuleTests(unittest.TestCase):
    def test_market_data_takes_scrapecreators_only_and_scrapecreators_takes_nothing_else(self):
        self.assertIsNone(binding_scope_mismatch("market_data", "scrapecreators"))
        self.assertIsNotNone(binding_scope_mismatch("market_data", "openai"))
        self.assertIsNotNone(binding_scope_mismatch("generation", "scrapecreators"))
        # Other scopes keep their own rules.
        self.assertIsNone(binding_scope_mismatch("generation", "openai"))
        self.assertIsNone(binding_scope_mismatch("workspace", "meta"))


class MarketDataCredentialApiTests(LibraryFixtures):
    """The Settings page's commands for a ScrapeCreators key, over HTTP."""

    def setUp(self) -> None:
        self.ctx = self.owner_ctx(self._make_context(role="owner", actor="owner_a"))
        app.dependency_overrides[request_tenant_context] = lambda: self.ctx
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        base_url = patch.object(config, "SCRAPECREATORS_BASE_URL", "https://scrapecreators.test")
        base_url.start()
        self.addCleanup(base_url.stop)

    # -- helpers ----------------------------------------------------------

    def _with_vendor(self, *responses) -> _Vendor:
        vendor = _Vendor(*responses)
        patcher = vendor.patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        return vendor

    def _create(self, plaintext: bytes = FIXTURE_KEY, *, key: str = KEY_NAME,
                provider: str = "scrapecreators", label: str | None = "Scan key"):
        return self.client.post("/api/v1/credentials", json={
            "credential_key": key, "provider": provider,
            "plaintext_b64": _b64(plaintext), "display_label": label,
        })

    def _rotate(self, plaintext: bytes, key: str = KEY_NAME):
        return self.client.post(
            f"/api/v1/credentials/{key}/rotate", json={"new_plaintext_b64": _b64(plaintext)},
        )

    def _bind(self, version_id: str, provider: str = "scrapecreators",
              scope: str = MARKET_DATA_BINDING_SCOPE):
        return self.client.put(
            f"/api/v1/credentials/{version_id}/bindings/{scope}", json={"provider": provider},
        )

    def _revoke(self, version_id: str):
        return self.client.post(
            f"/api/v1/credentials/{version_id}/revoke", json={"reason": "fixture-only"},
        )

    def _listed(self) -> list[dict]:
        response = self.client.get("/api/v1/credentials")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _assert_problem(self, response, status: int, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        self.assertIn("application/problem+json", response.headers["content-type"])
        body = response.json()
        self.assertEqual(body["code"], code)
        return body

    def _assert_no_secret(self, text: str, *secrets: bytes) -> None:
        for secret in secrets or (FIXTURE_KEY,):
            self.assertNotIn(secret.decode(), text)
            self.assertNotIn(_b64(secret), text)

    def _check_events(self) -> list[AuditEventRecord]:
        with tenant_session(self.ctx) as session:
            return list(session.scalars(
                select(AuditEventRecord)
                .where(AuditEventRecord.workspace_id == self.ctx.workspace_id,
                       AuditEventRecord.event_type == CHECK_EVENT)
                .order_by(AuditEventRecord.created_at)
            ))

    def _resolved(self) -> dict:
        with tenant_session(self.ctx) as session:
            return resolve_market_data_credential(session, self.ctx, "scrapecreators")

    # -- add / enable / replace / remove --------------------------------------

    def test_a_key_with_credits_is_checked_stored_and_bound_for_market_data(self):
        vendor = self._with_vendor(_balance(24987))
        created = self._create()
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual((body["provider"], body["credential_key"], body["state"]),
                         ("scrapecreators", KEY_NAME, "active"))
        self.assertEqual(body["bindings"], [])
        self.assertEqual(body["last_check"]["outcome"], "valid")
        self.assertEqual(body["last_check"]["credit_count"], 24987)
        self._assert_no_secret(created.text)
        self.assertEqual(len(vendor.requests), 1)
        self.assertEqual(vendor.requests[0].headers["x-api-key"], FIXTURE_KEY.decode())
        version_id = body["credential_version_id"]

        # Saved but not yet enabled: nothing resolves for market data.
        self.assertEqual(self._resolved(), {})
        bound = self._bind(version_id)
        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertEqual(bound.json()["binding_scope"], "market_data")
        self.assertEqual(self._resolved(),
                         {"active": True, "provider": "scrapecreators", "version_id": version_id})

        (listed,) = self._listed()
        self.assertEqual(listed["bindings"], [{"binding_scope": "market_data", "provider": "scrapecreators"}])
        self.assertEqual(listed["last_check"]["outcome"], "valid")
        self.assertEqual(listed["last_check"]["credit_count"], 24987)

        # The evidence names the version and never the key.
        (event,) = self._check_events()
        self.assertEqual((event.resource_type, event.resource_id), ("credential_version", version_id))
        self.assertEqual(event.details_json["outcome"], "valid")
        self.assertEqual(event.details_json["credit_count"], 24987)
        self._assert_no_secret(json.dumps(event.details_json))

        # A market-data key never shows up as a generation provider.
        capabilities = self.client.get("/api/v1/generation/capabilities").json()
        self.assertNotIn("scrapecreators", capabilities["credential_readiness"])
        self.assertNotIn("scrapecreators", capabilities["providers"])

    def test_an_invalid_or_empty_key_is_refused_and_nothing_is_stored(self):
        self._with_vendor(_balance(0))
        refused = self._create()
        self._assert_problem(refused, 422, "market_data_credential_invalid")
        self._assert_no_secret(refused.text)
        self.assertEqual(self._listed(), [])
        self.assertEqual(self._resolved(), {})
        (event,) = self._check_events()
        self.assertEqual((event.resource_type, event.resource_id), ("credential_key", KEY_NAME))
        self.assertEqual(event.details_json["outcome"], "invalid_or_no_balance")
        self._assert_no_secret(json.dumps(event.details_json))

    def test_a_check_that_could_not_run_stores_nothing(self):
        self._with_vendor(httpx.Response(503, text="maintenance"))
        self._assert_problem(self._create(), 503, "market_data_credential_check_unavailable")
        self.assertEqual(self._listed(), [])
        (event,) = self._check_events()
        self.assertEqual(event.details_json["outcome"], "unavailable")

    def test_replacing_checks_the_new_key_and_a_refused_one_keeps_the_current_key(self):
        vendor = self._with_vendor(_balance(50), _balance(0), _balance(7))
        first = self._create().json()["credential_version_id"]
        self.assertEqual(self._bind(first).status_code, 200)

        refused = self._rotate(b"fixture-empty-key")
        self._assert_problem(refused, 422, "market_data_credential_invalid")
        self._assert_no_secret(refused.text, b"fixture-empty-key")
        (still,) = self._listed()
        self.assertEqual(still["credential_version_id"], first)
        self.assertEqual(self._resolved()["version_id"], first)

        rotated = self._rotate(b"fixture-topped-up-key")
        self.assertEqual(rotated.status_code, 200, rotated.text)
        second = rotated.json()
        self.assertEqual((second["version_no"], second["provider"]), (2, "scrapecreators"))
        self.assertEqual(second["last_check"]["credit_count"], 7)
        self.assertEqual([r.headers["x-api-key"] for r in vendor.requests], [
            FIXTURE_KEY.decode(), "fixture-empty-key", "fixture-topped-up-key",
        ])
        # Like generation, the binding still names the rotated version
        # until the new one is enabled.
        self.assertEqual(self._resolved(), {})
        self.assertEqual(self._bind(second["credential_version_id"]).status_code, 200)
        self.assertEqual(self._resolved()["version_id"], second["credential_version_id"])

    def test_removing_the_key_withdraws_it(self):
        self._with_vendor(_balance(50))
        version_id = self._create().json()["credential_version_id"]
        self.assertEqual(self._bind(version_id).status_code, 200)
        self.assertEqual(self._revoke(version_id).status_code, 200)
        self.assertEqual(self._listed(), [])
        self.assertEqual(self._resolved(), {})

    def test_a_duplicate_is_refused_before_the_vendor_is_asked(self):
        vendor = self._with_vendor(_balance(50))
        self.assertEqual(self._create().status_code, 200)
        self._assert_problem(self._create(b"fixture-second-key"), 409, "credential_key_active")
        self.assertEqual(len(vendor.requests), 1)

    def test_image_provider_keys_are_never_sent_to_the_vendor(self):
        vendor = self._with_vendor(_balance(50))
        created = self._create(b"fixture-openai-only", key="openai_api_key", provider="openai")
        self.assertEqual(created.status_code, 200, created.text)
        self.assertIsNone(created.json()["last_check"])
        rotated = self._rotate(b"fixture-openai-rotated", key="openai_api_key")
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertEqual(vendor.requests, [])
        self.assertEqual(self._check_events(), [])

    # -- scope rules -------------------------------------------------------

    def test_scope_and_provider_must_match(self):
        self._with_vendor(_balance(50))
        scan_key = self._create().json()["credential_version_id"]
        self._assert_problem(self._bind(scan_key, scope="generation"),
                             409, "credential_scope_provider_mismatch")
        openai = self._create(b"fixture-openai-only", key="openai_api_key", provider="openai").json()
        self._assert_problem(self._bind(openai["credential_version_id"], provider="openai"),
                             409, "credential_scope_provider_mismatch")
        # A provider that does not match the version stays "not bindable".
        self._assert_problem(self._bind(openai["credential_version_id"], provider="scrapecreators"),
                             409, "credential_not_bindable")
        self.assertTrue(all(c["bindings"] == [] for c in self._listed()))

    # -- authorization and isolation ----------------------------------------

    def test_members_cannot_manage_market_data_keys_and_cause_no_vendor_call(self):
        vendor = self._with_vendor(_balance(50))
        version_id = self._create().json()["credential_version_id"]
        self.assertEqual(self._bind(version_id).status_code, 200)
        calls_before = len(vendor.requests)
        owner = self.ctx
        self.ctx = self._make_context(owner.workspace_id, role="member", actor="member_b")
        self._assert_problem(self.client.get("/api/v1/credentials"), 403, "owner_role_required")
        self._assert_problem(self._create(b"fixture-member-key", key="member_key"), 403, "owner_role_required")
        self._assert_problem(self._rotate(b"fixture-member-key"), 403, "owner_role_required")
        self._assert_problem(self._bind(version_id), 403, "owner_role_required")
        self._assert_problem(self._revoke(version_id), 403, "owner_role_required")
        self.assertEqual(len(vendor.requests), calls_before)
        self.ctx = owner
        (listed,) = self._listed()
        self.assertEqual(listed["credential_version_id"], version_id)
        self.assertEqual(self._resolved()["version_id"], version_id)

    def test_another_workspace_cannot_read_or_use_the_key(self):
        vendor = self._with_vendor(_balance(50))
        version_id = self._create().json()["credential_version_id"]
        self.assertEqual(self._bind(version_id).status_code, 200)
        calls_before = len(vendor.requests)
        owner = self.ctx
        self.ctx = self.owner_ctx(self._make_context(role="owner", actor="other_owner"))
        self.assertEqual(self._listed(), [])
        self.assertEqual(self._resolved(), {})
        self._assert_problem(self._bind(version_id), 404, "credential_not_found")
        self._assert_problem(self._rotate(b"fixture-foreign-key"), 404, "credential_not_found")
        self._assert_problem(self._revoke(version_id), 404, "credential_not_found")
        self.assertEqual(len(vendor.requests), calls_before)
        self.ctx = owner
        self.assertEqual(self._resolved()["version_id"], version_id)
        (listed,) = self._listed()
        self.assertEqual(listed["last_check"]["outcome"], "valid")


if __name__ == "__main__":
    unittest.main()
