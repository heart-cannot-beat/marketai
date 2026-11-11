"""The internal_validation read adapter and its GET-only gateway (#112, #101 S6).

Simulated HTTP only (``tests/support/meta_graph_simulator.py``): no network,
no real token. Covered here:

* the gateway sends GET and nothing else, the token as a header and its
  ``appsecret_proof`` as a query parameter, follows no redirect, refuses any
  path but an ad account's read edges, and names no secret in an error;
* Meta's usage headers and the back-off they lead to; the error codes that
  mean an expired or revoked token, a missing permission, a rate limit or a
  transient failure;
* the adapter decrypts the binding's two CredentialVersions on every read,
  only while they are active and only in the platform Workspace, pages with
  Meta's cursors, and keeps no credential in a page -- not even the token
  Meta writes into ``paging.next``;
* it has no write method, and never reads ``META_WRITE_ACCESS_TOKEN`` or the
  process-wide read credentials;
* ``resolve_adapter`` hands it out for the platform Workspace alone.

``tests/test_meta_internal_validation_sync.py`` runs it through the Worker.
"""
from __future__ import annotations

import json
import logging
import unittest
import uuid
from datetime import date
from pathlib import Path
from unittest import mock

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import update

from app import config
from app.adapters.meta import client as meta_client
from app.adapters.meta import http_read_adapter
from app.adapters.meta.client import (
    BACKOFF_ELEVATED_SECONDS,
    BACKOFF_HIGH_SECONDS,
    BACKOFF_THROTTLED_SECONDS,
    MAX_BACKOFF_SECONDS,
    MIN_BACKOFF_SECONDS,
    HttpMetaReadGateway,
    MetaReadCredentials,
    MetaReadError,
    MetaUsage,
    parse_usage_headers,
)
from app.adapters.meta.http_read_adapter import HttpMetaReadAdapter, credential_opener
from app.adapters.persistence.database import engine, platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import LocalKmsAdapter
from app.entitlements.credentials import CredentialVersionRecord
from app.meta.credential_resolution import AdapterResolutionError, resolve_adapter
from app.meta.models import MetaDataSourceBindingRecord
from app.meta.scale_read_contracts import AccountBinding, CONFIGURATION_EDGES, META_GRAPH_API_VERSION
from app.meta.scale_read_parsers import parse_daily, parse_delivery_configuration
from tests.support.meta_graph_simulator import (
    ACCESS_TOKEN,
    AD_ACCOUNT,
    APP_SECRET,
    CURRENCY,
    TIMEZONE,
    GraphSimulator,
    business_usage,
    graph_error,
    platform_context,
    provision_connection,
)

CREDENTIALS = MetaReadCredentials(ACCESS_TOKEN, APP_SECRET)
DAILY_FIELDS = ("account_id", "account_currency", "ad_id", "spend", "impressions", "inline_link_clicks",
                "actions", "action_values")
SECRETS = CREDENTIALS.secrets()


def _assert_no_secret(test: unittest.TestCase, value) -> None:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for secret in SECRETS:
        test.assertNotIn(secret, text)


def _gateway(handler) -> HttpMetaReadGateway:
    return HttpMetaReadGateway(api_version=META_GRAPH_API_VERSION, timeout=5.0,
                               transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------


class GatewayTests(unittest.TestCase):
    def test_a_read_is_one_get_with_the_token_in_a_header_and_the_proof_in_the_query(self):
        graph = GraphSimulator()
        response = graph.gateway().get(f"/{AD_ACCOUNT}", {"fields": "currency"}, CREDENTIALS)

        self.assertEqual(response.http_status, 200)
        request, = graph.requests
        self.assertEqual((request.method, request.path), ("GET", f"/{META_GRAPH_API_VERSION}/{AD_ACCOUNT}"))
        self.assertTrue(request.url.startswith("https://graph.facebook.com/"))
        self.assertEqual(request.authorization, f"Bearer {ACCESS_TOKEN}")
        self.assertEqual(request.params, {"fields": "currency", "appsecret_proof": CREDENTIALS.appsecret_proof()})
        self.assertNotIn(ACCESS_TOKEN, request.url)
        self.assertNotIn(APP_SECRET, request.url)

    def test_only_an_ad_accounts_read_edges_are_requested(self):
        graph = GraphSimulator()
        for path in ("/me", f"/{AD_ACCOUNT}/adcreatives", f"/{AD_ACCOUNT}/ads/extra", "https://evil.example/x",
                     f"{AD_ACCOUNT}/ads", f"/{AD_ACCOUNT}/../me", "/act_1?x=1"):
            with self.subTest(path=path), self.assertRaises(MetaReadError) as refused:
                graph.gateway().get(path, {}, CREDENTIALS)
            self.assertEqual((refused.exception.code, refused.exception.network_attempted),
                             ("meta_read_request_invalid", False))
        self.assertEqual(graph.requests, [])

    def test_credentials_never_ride_in_the_parameters(self):
        graph = GraphSimulator()
        for key in ("access_token", "appsecret_proof"):
            with self.subTest(key=key), self.assertRaises(MetaReadError):
                graph.gateway().get(f"/{AD_ACCOUNT}", {key: "x"}, CREDENTIALS)
        self.assertEqual(graph.requests, [])

    def test_a_redirect_is_not_followed(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://elsewhere.example/steal"})

        with self.assertRaises(MetaReadError) as failed:
            _gateway(handler).get(f"/{AD_ACCOUNT}", {}, CREDENTIALS)
        self.assertEqual(len(seen), 1)
        self.assertEqual(failed.exception.code, "meta_read_response_invalid")

    def test_a_network_failure_names_no_secret(self):
        def handler(request):
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        with self.assertRaises(MetaReadError) as failed:
            _gateway(handler).get(f"/{AD_ACCOUNT}", {}, CREDENTIALS)
        self.assertEqual((failed.exception.code, failed.exception.kind), ("meta_read_unavailable", "transient"))
        self.assertIsNone(failed.exception.__cause__)
        self.assertIsNone(failed.exception.__context__ if failed.exception.__suppress_context__ is False else None)
        _assert_no_secret(self, str(failed.exception))

    def test_meta_errors_are_classified_and_scrubbed(self):
        echo = f"Invalid token {ACCESS_TOKEN} with proof {CREDENTIALS.appsecret_proof()}"
        cases = [
            (graph_error(400, 190, echo, subcode=463), "meta_read_auth_failed", "auth", True),
            (graph_error(401, 102, "Session key invalid"), "meta_read_auth_failed", "auth", True),
            (graph_error(403, 10, "Permission denied"), "meta_read_permission_denied", "auth", True),
            (graph_error(400, 200, "Requires ads_read"), "meta_read_permission_denied", "auth", True),
            (graph_error(400, 17, "User request limit reached"), "meta_read_rate_limited", "throttled", False),
            (graph_error(400, 80000, "Too many calls to this ad account"), "meta_read_rate_limited", "throttled",
             False),
            (graph_error(400, 100, "Busy", subcode=1504022), "meta_read_rate_limited", "throttled", False),
            (httpx.Response(429, text="slow down"), "meta_read_rate_limited", "throttled", False),
            (graph_error(500, 2, "Service temporarily unavailable"), "meta_read_unavailable", "transient", False),
            (httpx.Response(503, text="<html>"), "meta_read_unavailable", "transient", False),
            (graph_error(400, 100, "Invalid parameter"), "meta_read_failed", "failed", False),
            (httpx.Response(200, text="not json"), "meta_read_response_invalid", "failed", False),
            (httpx.Response(200, json=["a", "list"]), "meta_read_response_invalid", "failed", False),
        ]
        for response, code, kind, permanent in cases:
            with self.subTest(code=code, status=response.status_code):
                with self.assertRaises(MetaReadError) as failed:
                    _gateway(lambda _request, r=response: r).get(f"/{AD_ACCOUNT}", {}, CREDENTIALS)
                error = failed.exception
                self.assertEqual((error.code, error.kind, error.permanent), (code, kind, permanent))
                self.assertEqual(error.http_status, response.status_code)
                _assert_no_secret(self, str(error))
                if kind == "throttled":
                    self.assertEqual(error.error_category, "rate_limit")
                    self.assertEqual(error.retry_after, BACKOFF_THROTTLED_SECONDS)

    def test_a_rate_limit_error_waits_as_long_as_meta_says(self):
        with self.assertRaises(MetaReadError) as told:
            _gateway(lambda _r: graph_error(400, 80000, "Too many calls", headers=business_usage(100, regain_minutes=20))) \
                .get(f"/{AD_ACCOUNT}", {}, CREDENTIALS)
        self.assertEqual(told.exception.retry_after, 20 * 60)
        with self.assertRaises(MetaReadError) as retry_after:
            _gateway(lambda _r: httpx.Response(429, headers={"retry-after": "900"})).get(f"/{AD_ACCOUNT}", {}, CREDENTIALS)
        self.assertEqual(retry_after.exception.retry_after, 900)
        with self.assertRaises(MetaReadError) as short:
            _gateway(lambda _r: httpx.Response(429, headers={"retry-after": "5"})).get(f"/{AD_ACCOUNT}", {}, CREDENTIALS)
        self.assertEqual(short.exception.retry_after, MIN_BACKOFF_SECONDS)

    def test_the_gateway_holds_no_credential_and_reads_no_configured_one(self):
        graph = GraphSimulator()
        sentinels = {
            "META_WRITE_ACCESS_TOKEN": "ci-only-write-token-must-not-be-read",
            "META_ACCESS_TOKEN": "ci-only-legacy-read-token-must-not-be-read",
            "META_APP_SECRET": "ci-only-legacy-secret-must-not-be-read",
        }
        with mock.patch.multiple(config, **sentinels):
            gateway = graph.gateway()
            gateway.get(f"/{AD_ACCOUNT}", {}, CREDENTIALS)
        self.assertEqual([key for key in vars(gateway) if "token" in key.lower() or "secret" in key.lower()], [])
        for sentinel in sentinels.values():
            self.assertNotIn(sentinel, json.dumps([vars(request) for request in graph.requests]))


class UsageTests(unittest.TestCase):
    def test_each_usage_header_is_read_and_the_highest_reading_counts(self):
        usage = parse_usage_headers({
            "X-App-Usage": json.dumps({"call_count": 12, "total_cputime": 40, "total_time": 31}),
            "X-Ad-Account-Usage": json.dumps({"acc_id_util_pct": 22.5, "reset_time_duration": 0}),
            "X-FB-Ads-Insights-Throttle": json.dumps({"app_id_util_pct": 61, "acc_id_util_pct": 8}),
            **business_usage(55),
        })
        self.assertEqual(usage.max_utilisation_pct, 61)
        self.assertEqual(set(usage.reported_by), {"x-app-usage", "x-ad-account-usage",
                                                  "x-fb-ads-insights-throttle", "x-business-use-case-usage"})
        self.assertIsNone(usage.backoff_seconds())

    def test_the_backoff_starts_at_75_percent(self):
        cases = [
            (74.9, None, None),
            (75, None, BACKOFF_ELEVATED_SECONDS),
            (89, None, BACKOFF_ELEVATED_SECONDS),
            (90, None, BACKOFF_HIGH_SECONDS),
            (100, None, BACKOFF_HIGH_SECONDS),
            (80, 40 * 60, 40 * 60),
            (95, 3 * 3600, MAX_BACKOFF_SECONDS),
        ]
        for peak, regain, expected in cases:
            with self.subTest(peak=peak, regain=regain):
                self.assertEqual(MetaUsage(peak, regain).backoff_seconds(), expected)
        self.assertEqual((BACKOFF_ELEVATED_SECONDS, BACKOFF_HIGH_SECONDS, BACKOFF_THROTTLED_SECONDS),
                         (900, 1800, 3600))

    def test_meta_regain_estimates_are_read_from_both_headers(self):
        self.assertEqual(parse_usage_headers(business_usage(96, regain_minutes=12)).regain_after_seconds, 720)
        self.assertEqual(parse_usage_headers({"x-ad-account-usage": json.dumps(
            {"acc_id_util_pct": 99, "reset_time_duration": 1500})}).regain_after_seconds, 1500)

    def test_a_header_that_does_not_parse_is_ignored(self):
        usage = parse_usage_headers({"x-app-usage": "{not json", "x-ad-account-usage": json.dumps(["x"]),
                                     "x-business-use-case-usage": json.dumps({"b": "oops"})})
        self.assertEqual((usage.max_utilisation_pct, usage.reported_by), (None, ()))
        self.assertIsNone(usage.backoff_seconds())


# ---------------------------------------------------------------------------
# The adapter, with credentials in the database
# ---------------------------------------------------------------------------


class AdapterTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db_file = engine.url.database
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")

    def setUp(self) -> None:
        self.workspace_id = f"ws_meta_read_{uuid.uuid4().hex[:12]}"
        with platform_session() as session:
            session.add(WorkspaceRecord(id=self.workspace_id, name="Acme Charge platform",
                                        is_platform_workspace=True))
        self.context = platform_context(self.workspace_id)
        self.provisioned = provision_connection(self.context)
        self.graph = GraphSimulator()

    def _adapter(self, session, protector=None) -> HttpMetaReadAdapter:
        return HttpMetaReadAdapter(
            open_credentials=credential_opener(
                session, workspace_id=self.workspace_id,
                access_token_version_id=self.provisioned["access_token_version_id"],
                app_secret_version_id=self.provisioned["app_secret_version_id"],
                protector=protector,
            ),
            gateway=self.graph.gateway(),
        )

    def _insights(self, adapter, after=None, **overrides):
        request = dict(ad_account_id=AD_ACCOUNT, fields=list(DAILY_FIELDS),
                       since=date(2026, 10, 28), until=date(2026, 11, 10), limit=25, after=after)
        request.update(overrides)
        return adapter.read_insights(**request)


class AdapterReadTests(AdapterTestCase):
    def test_insights_page_with_metas_cursors_and_keep_no_credential(self):
        with tenant_session(self.context) as session:
            adapter = self._adapter(session)
            pages = [self._insights(adapter, time_increment="1", action_attribution_windows=["7d_click", "1d_view"],
                                    action_report_time="impression")]
            while pages[-1].next_cursor:
                pages.append(self._insights(adapter, after=pages[-1].next_cursor, time_increment="1",
                                            action_attribution_windows=["7d_click", "1d_view"],
                                            action_report_time="impression"))

        self.assertEqual([page.row_count for page in pages], [25, 17])  # 14 days x 3 ads
        self.assertIsNone(pages[-1].next_cursor, "the last page still names `after`; only `next` means more")
        first, second = self.graph.requests
        self.assertEqual(first.params, {
            "level": "ad", "fields": ",".join(DAILY_FIELDS),
            "time_range": '{"since":"2026-10-28","until":"2026-11-10"}', "time_increment": "1", "limit": "25",
            "action_attribution_windows": '["7d_click", "1d_view"]', "action_report_time": "impression",
            "appsecret_proof": CREDENTIALS.appsecret_proof(),
        })
        self.assertEqual(second.params["after"], pages[0].next_cursor)
        for page in pages:
            self.assertNotIn("next", page.payload.get("paging", {}))
            self.assertNotIn("previous", page.payload.get("paging", {}))
            _assert_no_secret(self, page.payload)
        self.assertEqual(set(pages[0].payload["paging"]["cursors"]), {"before", "after"})
        parsed = parse_daily([page.payload for page in pages],
                             expected=AccountBinding(AD_ACCOUNT, CURRENCY, TIMEZONE), ad_id="acme_ad_2",
                             since=date(2026, 10, 28), until=date(2026, 11, 10))
        self.assertEqual(len(parsed.days), 14)

    def test_the_configuration_edges_parse_as_the_scale_contract_reads_them(self):
        with tenant_session(self.context) as session:
            adapter = self._adapter(session)
            edge_pages = []
            for edge, fields in CONFIGURATION_EDGES:
                page = adapter.read_delivery_configuration(ad_account_id=AD_ACCOUNT, edge=edge, fields=list(fields),
                                                           as_of=date(2026, 11, 10))
                self.assertIsNone(page.next_cursor)
                edge_pages.append((edge, page.payload))
        self.assertEqual([request.path.split("/", 2)[-1] for request in self.graph.requests],
                         [AD_ACCOUNT, f"{AD_ACCOUNT}/ads", f"{AD_ACCOUNT}/adsets", f"{AD_ACCOUNT}/campaigns"])
        self.assertNotIn("limit", self.graph.requests[0].params, "the account is a node, not a list")
        self.assertEqual(edge_pages[0][1]["data"][0]["currency"], CURRENCY)
        observation = parse_delivery_configuration(edge_pages, expected=AccountBinding(AD_ACCOUNT, CURRENCY, TIMEZONE),
                                                   ad_id="acme_ad_1")
        self.assertEqual(observation.ad_creative_id, "cr_acme_ad_1")

    def test_every_read_decrypts_both_credentials_afresh(self):
        protector = _CountingProtector()
        with tenant_session(self.context) as session:
            adapter = self._adapter(session, protector=protector)
            for _ in range(3):
                adapter.read_delivery_configuration(ad_account_id=AD_ACCOUNT, edge="account",
                                                    fields=["currency"], as_of=date(2026, 11, 10))
        self.assertEqual(protector.opened, ["meta_access_token", "meta_app_secret"] * 3)
        self.assertEqual(len(self.graph.requests), 3)
        self.assertNotIn(ACCESS_TOKEN, repr(adapter))
        self.assertNotIn(ACCESS_TOKEN, repr(CREDENTIALS) + str(CREDENTIALS))

    def test_a_revoked_or_foreign_credential_reads_nothing(self):
        admin = CredentialAdmin(LocalKmsAdapter.local_only())
        with tenant_session(self.context) as session:
            admin.revoke(session, self.context, credential_key="meta_access_token", reason="rotated by operator")
        with tenant_session(self.context) as session, self.assertRaises(MetaReadError) as refused:
            self._insights(self._adapter(session))
        self.assertEqual((refused.exception.code, refused.exception.permanent, refused.exception.network_attempted),
                         ("meta_read_credential_unavailable", True, False))
        self.assertEqual(self.graph.requests, [])

        with tenant_session(self.context) as session:
            adapter = HttpMetaReadAdapter(
                open_credentials=credential_opener(
                    session, workspace_id=self.workspace_id,
                    access_token_version_id=self.provisioned["app_secret_version_id"],  # the wrong key
                    app_secret_version_id=self.provisioned["app_secret_version_id"],
                ),
                gateway=self.graph.gateway(),
            )
            with self.assertRaises(MetaReadError) as wrong_key:
                self._insights(adapter)
        self.assertEqual(wrong_key.exception.code, "meta_read_credential_unavailable")
        self.assertEqual(self.graph.requests, [])

    def test_a_credential_sealed_under_another_key_does_not_open(self):
        with tenant_session(self.context) as session, self.assertRaises(MetaReadError) as refused:
            self._insights(self._adapter(session, protector=LocalKmsAdapter(b"ci-only-another-master-secret-32by!")))
        self.assertEqual(refused.exception.code, "meta_read_credential_unavailable")
        self.assertEqual(self.graph.requests, [])

    def test_a_workspace_that_is_not_the_platform_workspace_reads_nothing(self):
        with platform_session() as session:
            session.execute(update(WorkspaceRecord).where(WorkspaceRecord.id == self.workspace_id)
                            .values(is_platform_workspace=False))
        with tenant_session(self.context) as session, self.assertRaises(MetaReadError) as refused:
            self._insights(self._adapter(session))
        self.assertEqual((refused.exception.code, refused.exception.network_attempted),
                         ("meta_platform_workspace_required", False))
        self.assertEqual(self.graph.requests, [])

    def test_the_httpx_log_line_carries_no_secret(self):
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        logger = logging.getLogger("httpx")
        handler = Capture(level=logging.DEBUG)
        logger.addHandler(handler)
        previous = (logger.level, logger.disabled)
        # Alembic's fileConfig in setUpClass disables loggers that already exist.
        logger.setLevel(logging.DEBUG)
        logger.disabled = False
        try:
            with tenant_session(self.context) as session:
                self._insights(self._adapter(session))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous[0])
            logger.disabled = previous[1]
        self.assertTrue(any("graph.facebook.com" in line for line in records), records)
        for line in records:
            _assert_no_secret(self, line)


class NoWritePathTests(unittest.TestCase):
    """POST and every other write verb are unreachable from the adapter."""

    def test_the_adapter_has_only_the_two_reads(self):
        public = {name for name in dir(HttpMetaReadAdapter) if not name.startswith("_")}
        self.assertEqual(public, {"is_test_adapter", "read_insights", "read_delivery_configuration"})
        self.assertFalse(HttpMetaReadAdapter.is_test_adapter)

    def test_the_gateway_has_get_alone(self):
        verbs = {"get", "post", "put", "patch", "delete", "request", "send", "stream", "options", "head"}
        self.assertEqual({name for name in dir(HttpMetaReadGateway) if name in verbs}, {"get"})

    def test_every_request_a_read_sends_is_a_get(self):
        graph = GraphSimulator()
        with mock.patch.object(http_read_adapter, "read_gateway", graph.gateway):
            gateway = http_read_adapter.read_gateway()
            adapter = HttpMetaReadAdapter(open_credentials=lambda: _Credentials(), gateway=gateway)
            adapter.read_insights(ad_account_id=AD_ACCOUNT, fields=["spend"], since=date(2026, 11, 1),
                                  until=date(2026, 11, 2))
            for edge, fields in CONFIGURATION_EDGES:
                adapter.read_delivery_configuration(ad_account_id=AD_ACCOUNT, edge=edge, fields=list(fields),
                                                    as_of=date(2026, 11, 2))
        self.assertEqual({request.method for request in graph.requests}, {"GET"})
        self.assertEqual(len(graph.requests), 5)

    def test_the_read_contracts_api_version_is_the_one_requested(self):
        self.assertEqual(http_read_adapter.read_gateway().api_version, META_GRAPH_API_VERSION)


class _CountingProtector:
    """The local protector, counting what it opens."""

    def __init__(self) -> None:
        self._inner = LocalKmsAdapter.local_only()
        self.opened: list[str] = []

    def open(self, envelope, context):
        self.opened.append(context.credential_key)
        return self._inner.open(envelope, context)


class _Credentials:
    def __enter__(self):
        return CREDENTIALS

    def __exit__(self, *_exc):
        return False


# ---------------------------------------------------------------------------
# resolve_adapter
# ---------------------------------------------------------------------------


class ResolveAdapterTests(AdapterTestCase):
    def _resolve(self, session, *, read_only: bool = True):
        return resolve_adapter(session, workspace_id=self.workspace_id, meta_mode="internal_validation",
                               connection_id=self.provisioned["connection_id"], ad_account_id=AD_ACCOUNT,
                               read_only=read_only)

    def test_a_caller_that_may_write_is_refused_as_before(self):
        # The Launch saga resolves without read_only: internal_validation
        # never hands it an adapter, so no write intent is ever committed.
        with tenant_session(self.context) as session, self.assertRaises(AdapterResolutionError) as refused:
            resolve_adapter(session, workspace_id=self.workspace_id, meta_mode="internal_validation",
                            connection_id=self.provisioned["connection_id"], ad_account_id=AD_ACCOUNT)
        self.assertEqual((refused.exception.code, refused.exception.permanent), ("meta_read_only_mode", True))
        self.assertEqual(self.graph.requests, [])

    def test_the_platform_workspace_gets_the_read_adapter(self):
        with tenant_session(self.context) as session:
            resolved = self._resolve(session)
            self.assertIsInstance(resolved.adapter, HttpMetaReadAdapter)
            self.assertEqual((resolved.access_token_version_id, resolved.app_secret_version_id,
                              resolved.scenario_version_id),
                             (self.provisioned["access_token_version_id"], self.provisioned["app_secret_version_id"],
                              None))
            with mock.patch.object(http_read_adapter, "read_gateway", self.graph.gateway):
                page = self._resolve(session).adapter.read_delivery_configuration(
                    ad_account_id=AD_ACCOUNT, edge="account", fields=["currency"], as_of=date(2026, 11, 10))
        self.assertEqual(page.payload["data"][0]["currency"], CURRENCY)

    def test_another_workspace_is_refused_before_any_request(self):
        with platform_session() as session:
            session.execute(update(WorkspaceRecord).where(WorkspaceRecord.id == self.workspace_id)
                            .values(is_platform_workspace=False))
        with tenant_session(self.context) as session, self.assertRaises(AdapterResolutionError) as refused:
            self._resolve(session)
        self.assertEqual((refused.exception.code, refused.exception.permanent),
                         ("meta_platform_workspace_required", True))
        self.assertEqual(self.graph.requests, [])

    def test_a_binding_without_both_credentials_is_refused(self):
        with platform_session() as session:
            session.execute(update(MetaDataSourceBindingRecord)
                            .where(MetaDataSourceBindingRecord.id == self.provisioned["binding_id"])
                            .values(app_secret_version_id=None))
        with tenant_session(self.context) as session, self.assertRaises(AdapterResolutionError) as refused:
            self._resolve(session)
        self.assertEqual((refused.exception.code, refused.exception.permanent),
                         ("meta_read_credential_unavailable", True))

    def test_the_stored_credentials_are_ciphertext(self):
        with platform_session() as session:
            for version_id in (self.provisioned["access_token_version_id"], self.provisioned["app_secret_version_id"]):
                version = session.get(CredentialVersionRecord, version_id)
                for secret in (ACCESS_TOKEN.encode(), APP_SECRET.encode()):
                    self.assertNotIn(secret, version.encrypted_secret)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
