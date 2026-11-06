"""Issue #270 (C-21) -- competitor scans: quote, reservation, states, resume, reconciliation.

Real HTTP routes (``TestClient``) and the real Worker (``WorkerRuntime``)
with the Controlled Mock over the P0 recordings. "byok" runs put a mock in
place of ScrapeCreators and a fake balance check in place of the free
``credit-balance`` call: no real key, no real request, but reservations,
the fence, settlement by ``credits_charged``, releases and reconciliation
run for real.

Spec scenarios covered: 3 (two-line quote, one Operation, settle <= reserve,
``credits_charged`` per page), 4 (402 -> partial, resumable), 5 (a page lost
after the fence -> uncertain, reconciled by the balance difference), 6
(expired media refreshed, then out of quota), 10 (either key missing ->
Mock only), 13 (partial resume: a competitor that failed, keyword ads that
landed first, a new key), 16 (a second scan, or a resolve, waits its turn).
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select, update
from sqlalchemy.exc import DatabaseError

import httpx

from tests.support.queue_hygiene import quarantine_foreign_jobs
from tests.test_competitors_api import BRIGHTVOLT, ACME, PRICES, CompetitorFixtures
from tests.test_test_plans_api import PROTECTOR, openai_completion

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import JobRecord, OperationRecord, utcnow
from app.competitors.models import (
    CompetitorAdOriginRecord,
    CompetitorAdRecord,
    CompetitorResolutionRecord,
    CompetitorScanRecord,
)
from app.competitors import clusters, discovery, relevance, tags
from app.competitors.scan_media import MockMediaFetcher
from app.competitors.sources.base import SourceMethod
from app.competitors.sources.mock import load_fixtures
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.market_data.calls import market_data_call_key
from app.adapters.blob_store_local import LocalFileBlobStore
from app.platform.operations import OperationState, transition_operation
from app.providers.dispatch import ExternalCallCoordinator
from app.providers.llm_chat import LlmChatClient, llm_call_key
from app.providers.llm_mock import MockLlmChatAdapter
from app.providers.openai_chat import OpenAIChatAdapter
from app.worker_runtime import WorkerRuntime

HALCYON = "100000000010002"
KEYWORDS = ["NACS adapter", "CCS1 adapter"]
PRICE_USD = "0.00188"


class Killed(BaseException):
    """The Worker process dies mid-request: past ``except Exception``, like SIGKILL."""


def expire_lease(ctx, job_id: str) -> None:
    """The killed Worker's lease lapses: only the clock moves; the queue's own
    recovery decides what becomes of the Job, left as the Worker left it (#327)."""
    with tenant_session(ctx) as session:
        job = session.get(JobRecord, job_id)
        assert job.state == "running", job.state
        job.lease_expires_at = utcnow() - timedelta(seconds=1)


def _fixture_ads() -> dict[str, set[str]]:
    """``ad_archive_id`` sets per recorded page or keyword (both pages)."""
    groups: dict[str, set[str]] = {}
    for fixture in load_fixtures().recorded.values():
        params = fixture.params or {}
        key = params.get("pageId") or params.get("query")
        if key is None or not isinstance(fixture.body, dict):
            continue
        items = fixture.body.get("results") or fixture.body.get("searchResults") or []
        groups.setdefault(key, set()).update(
            item["ad_archive_id"] for item in items if isinstance(item, dict) and "ad_archive_id" in item
        )
    return groups


class ScanFixtures(CompetitorFixtures):
    def setUp(self) -> None:
        super().setUp()
        self.balance: int | None = None
        self.media_clock = lambda: datetime.now(timezone.utc)
        self.wrap = None  # optional source wrapper for the next drain
        self.addCleanup(self._retire_uncertain)

    def _retire_uncertain(self) -> None:
        """Lost-page scenarios end ``uncertain`` by design; later tests share the
        queue and the Operation sweep, so this Workspace's are retired here."""
        with tenant_session(self.ctx) as session:
            for operation_id in session.scalars(select(OperationRecord.id).where(
                OperationRecord.workspace_id == self.ctx.workspace_id,
                OperationRecord.state == OperationState.UNCERTAIN.value,
            )).all():
                transition_operation(session, operation_id, OperationState.FAILED, "test cleanup", "system:test")
            session.execute(update(JobRecord).where(
                JobRecord.workspace_id == self.ctx.workspace_id, JobRecord.state == "uncertain",
            ).values(state="cancelled", finished_at=utcnow()))

    # -- environment --------------------------------------------------------

    def _source(self, mode: str):
        source = super()._source(mode)
        return self.wrap(source) if self.wrap else source

    async def _balance(self, mode, credential, source):
        return self.balance if self.balance is not None else getattr(source, "credits_remaining", None)

    def _fake_openai(self, blob_store=None) -> LlmChatClient:
        """byok judgments answered by a fake OpenAI the way the Mock answers them:
        reservations, attempts and settlement run for real, no request leaves.
        ``blob_store`` serves the archived images a tag call sends (#272).
        ``discovery_answers`` overrides the next discover answers only (#276),
        ``cluster_answers`` the next clustering answers only (#353)."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.openai_requests.append(body)
            if self.openai_answers:
                answer = self.openai_answers.pop(0)
                if answer is not None:  # ``None`` lets this request be answered like the Mock
                    return answer
            user = next(message["content"] for message in reversed(body["messages"]) if message["role"] == "user")
            if not isinstance(user, str):  # a vision request: text part first, then the images
                user = next(part["text"] for part in user if part.get("type") == "text")
            schema = body["response_format"]["json_schema"]
            if schema["name"] == discovery.SCHEMA_NAME and getattr(self, "discovery_answers", None):
                answer = self.discovery_answers.pop(0)
                if answer is not None:
                    return answer
            if schema["name"] == clusters.SCHEMA_NAME and getattr(self, "cluster_answers", None):
                answer = self.cluster_answers.pop(0)
                if answer is not None:
                    return answer
            if schema["name"] == discovery.SCHEMA_NAME:
                answer = discovery.mock_answer(user)
            elif schema["name"] == tags.SCHEMA_NAME:
                answer = tags.mock_answer(user, schema["schema"], hashlib.sha256(user.encode("utf-8")).hexdigest())
            elif schema["name"] == clusters.SCHEMA_NAME:
                answer = clusters.mock_answer(user)
            else:
                answer = relevance.mock_answer(user)
            return openai_completion(json.dumps(answer, ensure_ascii=False))

        return LlmChatClient(
            coordinator=ExternalCallCoordinator(tenant_session), session_factory=tenant_session, protector=PROTECTOR,
            blob_store=blob_store,
            adapters={
                "openai": OpenAIChatAdapter(base_url="https://openai.test/v1", timeout=5,
                                            transport=httpx.MockTransport(handler)),
                "mock": MockLlmChatAdapter(),
            },
        )

    def _runtime(self) -> WorkerRuntime:
        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner=f"scans-{uuid.uuid4().hex[:6]}")
        runtime.blob_store = LocalFileBlobStore(
            root=self.blob_root.name, public_base_url="http://blob.test", signing_secret=b"test-secret",
        )
        runtime.ad_library_source = self._source
        runtime._market_data_protector = PROTECTOR
        runtime.competitor_media_fetcher = lambda mode: MockMediaFetcher(lambda: self.media_clock())
        runtime.market_data_balance_checker = self._balance
        runtime.competitor_scan_retry_delays = (0.0, 0.0)
        runtime._llm_chat_client = self._fake_openai(runtime.blob_store)
        return runtime

    def _drain(self) -> None:
        runtime = self._runtime()
        for _ in range(60):
            if not asyncio.run(runtime.run_once()):
                break

    def _bind_both(self) -> None:
        self._bind_market_data()
        self._bind_ai()

    def _bind_ai(self) -> None:
        owner = self.owner_ctx(self.ctx)
        admin = CredentialAdmin(PROTECTOR)
        with tenant_session(owner) as session:
            meta = admin.create(session, owner, credential_key="OPENAI_API_KEY", provider="openai",
                                plaintext=b"sk-test-placeholder")
            admin.bind(session, owner, binding_scope="generation", provider="openai",
                       credential_version_id=meta.credential_version_id)

    def _rotate_market_data(self) -> str:
        owner = self.owner_ctx(self.ctx)
        admin = CredentialAdmin(PROTECTOR)
        with tenant_session(owner) as session:
            meta = admin.rotate(session, owner, credential_key="SCRAPECREATORS_API_KEY",
                                new_plaintext=b"sc-test-placeholder-2")
            admin.bind(session, owner, binding_scope="market_data", provider="scrapecreators",
                       credential_version_id=meta.credential_version_id)
        return meta.credential_version_id

    # -- competitors and requests --------------------------------------------

    def _confirmed(self, page_id: str, *, role: str = "competitor", product_id: str | None = None) -> str:
        competitor = self._competitor(f"https://www.facebook.com/profile.php?id={page_id}", role=role,
                                      product_id=product_id)
        self.assertEqual(competitor["confirmation_status"], "confirmed")
        return competitor["competitor_id"]

    def _three(self) -> list[str]:
        if getattr(self, "_three_ids", None) is None:
            self._three_ids = [self._confirmed(ACME), self._confirmed(BRIGHTVOLT), self._confirmed(HALCYON)]
        return self._three_ids

    def _body(self, competitor_ids: list[str] | None = None, *, keywords=KEYWORDS, **overrides) -> dict:
        return {"product_id": self.product_id, "competitor_ids": competitor_ids or self._three(),
                "keywords": list(keywords), **overrides}

    def _preview(self, body: dict, expect: int = 200) -> dict:
        response = self.client.post("/api/v1/competitor-scans/preview", json=body)
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _start(self, body: dict, *, key: str | None = None, expect: int = 202) -> dict:
        response = self.client.post("/api/v1/competitor-scans", json=body, headers=self._headers(key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _scan(self, scan_id: str, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/competitor-scans/{scan_id}")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _resume(self, scan_id: str, *, key: str | None = None, expect: int = 202) -> dict:
        response = self.client.post(f"/api/v1/competitor-scans/{scan_id}/resume", headers=self._headers(key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _resume_quote(self, scan_id: str, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/competitor-scans/{scan_id}/resume-quote")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _reconcile(self, scan_id: str, attempt_id: str, outcome: str, *, key: str | None = None):
        return self.client.post(f"/api/v1/competitor-scans/{scan_id}/reconcile",
                                json={"attempt_id": attempt_id, "outcome": outcome}, headers=self._headers(key))

    def _all_ads(self, scan_id: str, **params) -> list[dict]:
        items, cursor = [], None
        while True:
            query = {**params, "limit": 37, **({"cursor": cursor} if cursor else {})}
            response = self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads", params=query)
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            items += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                self.assertEqual(len(items), page["total"])
                return items

    def _scan_reservations(self, job_id: str) -> dict[str, SpendReservationRecord]:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == self.ctx.workspace_id,
                SpendReservationRecord.job_id == job_id,
            )).all()
            for row in rows:
                session.expunge(row)
            return {row.semantic_key_hash: row for row in rows}

    def _origins(self, scan_id: str) -> list[CompetitorAdOriginRecord]:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(CompetitorAdOriginRecord).where(
                CompetitorAdOriginRecord.scan_id == scan_id)).all()
            for row in rows:
                session.expunge(row)
            return rows

    def _record(self, scan_id: str) -> CompetitorScanRecord:
        with tenant_session(self.ctx) as session:
            record = session.get(CompetitorScanRecord, scan_id)
            session.expunge(record)
            return record

    @staticmethod
    def _spec_call_names(competitors: list[str], keywords: list[str], *, pages=3, keyword_pages=2,
                         refreshes=10, tagged=60, discover=30) -> tuple[set[str], set[str]]:
        """Spec §5.1 / §5.2, written out independently of ``scan_plan``."""
        market = {f"page:{cid}:{n}" for cid in competitors for n in range(1, pages + 1)}
        market |= {f"keyword:{k}:{n}" for k in range(1, len(keywords) + 1) for n in range(1, keyword_pages + 1)}
        market |= {f"ad_refresh:{i}" for i in range(1, refreshes + 1)}
        total_pages = len(competitors) * pages + len(keywords) * keyword_pages
        llm = {f"filter:{i}" for i in range(1, math.ceil(total_pages * 30 / 20) + 1)}
        llm |= {f"tag:{i}" for i in range(1, tagged + 1)} | {"cluster:1"}
        if keywords:
            llm |= {f"discover:{i}" for i in range(1, discover + 1)}
        return market, llm


class ScanQuoteTests(ScanFixtures):
    def test_preview_quotes_two_lines_and_only_mock_without_keys(self):
        quote = self._preview(self._body())
        self.assertEqual((quote["kind"], quote["execution_mode"], quote["available_modes"]),
                         ("scan", "mock", ["mock"]))
        self.assertEqual(quote["byok_unavailable_code"], "market_data_credential_missing")
        self.assertEqual(quote["byok_unavailable_params"], {"provider": "scrapecreators"})
        self.assertEqual(
            [(c["binding_scope"], c["ready"], c["code"]) for c in quote["credentials"]],
            [("market_data", False, "market_data_credential_missing"),
             ("generation", False, "competitor_scan_ai_credential_missing")],
        )
        market = quote["market_data"]
        self.assertEqual([(line["operation"], line["calls"], line["credits"]) for line in market["lines"]],
                         [("page", 9, 9), ("keyword_page", 4, 4), ("ad_refresh", 10, 10)])
        self.assertEqual((market["provider"], market["max_credits"], market["max_usd"], market["simulated_usd"]),
                         ("mock", 23, "0", "0.04324"))
        llm = quote["llm"]
        self.assertEqual([(line["step"], line["calls"]) for line in llm["lines"]],
                         [("filter", 20), ("tag", 60), ("cluster", 1), ("discover", 30)])
        self.assertEqual((llm["calls"], llm["max_usd"], quote["max_usd"]), (111, "0", "0"))
        self.assertIsNotNone(llm["simulated_usd"])
        self.assertEqual((quote["language"], quote["country"], quote["competitor_count"], quote["keyword_count"]),
                         ("en", "US", 3, 2))
        # Without keywords there is no discovery line and fewer filter calls.
        quote = self._preview(self._body(keywords=[]))
        self.assertEqual([(line["step"], line["calls"]) for line in quote["llm"]["lines"]],
                         [("filter", 14), ("tag", 60), ("cluster", 1)])

    def test_only_confirmed_active_competitors_of_the_product_are_scanned(self):
        unconfirmed = self._competitor("Acme Charge")["competitor_id"]
        response = self.client.post("/api/v1/competitor-scans/preview", json=self._body([unconfirmed]))
        self.assertEqual(self._problem(response, 422, "competitor_scan_competitor_unconfirmed")["params"],
                         {"competitor_id": unconfirmed})
        other_product = self._confirmed(ACME, product_id=self._product())
        self._problem(self.client.post("/api/v1/competitor-scans/preview", json=self._body([other_product])),
                      422, "competitor_scan_product_mismatch")
        archived = self._confirmed(BRIGHTVOLT)
        self.current = self.owner_ctx(self.ctx)
        self.client.post(f"/api/v1/competitors/{archived}/archive", headers=self._headers())
        self.current = self.ctx
        self._problem(self.client.post("/api/v1/competitor-scans", json=self._body([archived]),
                                       headers=self._headers()), 409, "competitor_archived")
        self._problem(self.client.post("/api/v1/competitor-scans/preview", json=self._body(["cmp_missing"])),
                      404, "competitor_not_found")
        self._problem(self.client.post("/api/v1/competitor-scans/preview",
                                       json={**self._body(), "product_id": "prd_missing"}), 404, "product_not_found")
        self._problem(self.client.post("/api/v1/competitor-scans/preview", json={**self._body(), "country": "XX"}),
                      422, "competitor_country_invalid")
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.scalars(select(CompetitorScanRecord.id).where(
                CompetitorScanRecord.workspace_id == self.ctx.workspace_id)).all(), [])

    def test_scenario_10_either_key_missing_offers_mock_only(self):
        self._bind_market_data()
        quote = self._preview(self._body())
        self.assertEqual((quote["execution_mode"], quote["byok_unavailable_code"]),
                         ("mock", "competitor_scan_ai_credential_missing"))
        self._problem(self.client.post("/api/v1/competitor-scans", json={**self._body(), "execution_mode": "byok"},
                                       headers=self._headers()), 409, "competitor_scan_ai_credential_missing")
        self._bind_ai()
        quote = self._preview(self._body())
        self.assertEqual((quote["execution_mode"], quote["available_modes"]), ("byok", ["byok", "mock"]))
        self.assertEqual((quote["market_data"]["max_usd"], quote["market_data"]["simulated_usd"]), ("0.04324", None))
        self.assertEqual(quote["llm"]["provider"], "openai")
        self.assertEqual({c["binding_scope"]: c["ready"] for c in quote["credentials"]},
                         {"market_data": True, "generation": True})

    def test_scenario_10_an_ai_key_alone_is_not_enough(self):
        self._bind_market_data()
        with tenant_session(self.owner_ctx(self.ctx)) as session:
            from app.entitlements.credentials import CredentialBindingRecord
            session.execute(CredentialBindingRecord.__table__.delete().where(
                CredentialBindingRecord.workspace_id == self.ctx.workspace_id))
        self._bind_ai()
        quote = self._preview(self._body())
        self.assertEqual((quote["execution_mode"], quote["byok_unavailable_code"]),
                         ("mock", "market_data_credential_missing"))

    def test_346_a_quote_line_carries_its_total_credits(self):
        two = {"scrapecreators": {op: {"credits": 2, "credit_usd": PRICE_USD}
                                  for op in ("page_search", "probe", "page", "keyword_page", "ad_refresh")}}
        with patch.object(config, "MARKET_DATA_MAX_COSTS_JSON", json.dumps(two)):
            market = self._preview(self._body())["market_data"]
        lines = [(line["operation"], line["calls"], line["credits"]) for line in market["lines"]]
        self.assertEqual(lines, [("page", 9, 18), ("keyword_page", 4, 8), ("ad_refresh", 10, 20)])
        self.assertEqual(market["max_credits"], sum(credits for _, _, credits in lines))

    def test_318_a_price_table_without_an_operation_names_it(self):
        self._bind_both()
        partial = {"scrapecreators": {op: {"credits": 1, "credit_usd": PRICE_USD}
                                      for op in ("page_search", "probe", "page", "keyword_page")}}
        with patch.object(config, "MARKET_DATA_MAX_COSTS_JSON", json.dumps(partial)):
            quote = self._preview(self._body())
            self.assertEqual((quote["execution_mode"], quote["byok_unavailable_code"]),
                             ("mock", "market_data_price_operation_missing"))
            self.assertEqual(quote["byok_unavailable_params"], {"provider": "scrapecreators",
                                                                "operations": "ad_refresh"})
            body = self._problem(self.client.post("/api/v1/competitor-scans/preview",
                                                  json={**self._body(), "execution_mode": "byok"}),
                                 409, "market_data_price_operation_missing")
            self.assertEqual(body["params"]["operations"], "ad_refresh")

    def test_the_interface_language_is_frozen_into_the_params(self):
        self.current = dataclasses.replace(self.ctx, ui_language="zh-CN")
        self.assertEqual(self._preview(self._body())["language"], "zh-CN")
        accepted = self._start(self._body())
        self.assertEqual(accepted["params"]["language"], "zh-CN")
        self.current = self.ctx
        self.assertEqual(self._scan(accepted["scan_id"])["params"]["language"], "zh-CN")

    def test_the_guardrail_sees_both_lines_together(self):
        self._bind_both()
        quote = self._preview(self._body())
        with tenant_session(self.owner_ctx(self.ctx)) as session:
            from app.generation.spend import ensure_spend_guardrail
            guardrail = ensure_spend_guardrail(session, self.owner_ctx(self.ctx))
            # Above either line alone, below their sum.
            from decimal import Decimal
            guardrail.per_operation_limit = Decimal(quote["llm"]["max_usd"]) + Decimal("0.001")
        self._problem(self.client.post("/api/v1/competitor-scans", json=self._body(), headers=self._headers()),
                      409, "guardrail_exceeded")
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.scalars(select(SpendReservationRecord.id).where(
                SpendReservationRecord.workspace_id == self.ctx.workspace_id,
                SpendReservationRecord.state == "reserved")).all(), [])


class MockScanTests(ScanFixtures):
    def test_three_competitors_three_pages_and_two_keywords(self):
        acme, brightvolt, halcyon = self._three()
        accepted = self._start(self._body())
        self.assertEqual((accepted["status"], accepted["execution_mode"], accepted["provider"]),
                         ("queued", "mock", "mock"))
        self.assertEqual(self._scan_reservations(accepted["job_id"]), {})  # Mock reserves nothing real
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual((scan["status"], scan["stage"]), ("completed", "freeze"), scan["failure"])
        self.assertEqual([(s["step"], s["status"], s["reason"]) for s in scan["steps"]], [
            ("fetch", "completed", None), ("media", "completed", None),
            ("filter", "completed", None), ("tag", "completed", None),  # filter: C-22 (#271)
            ("cluster", "completed", None), ("freeze", "completed", None),  # tag, cluster, freeze: C-23 (#272)
            # (tag: every product has the expression mechanism catalog's factor, #329)
        ])
        targets = {t["origin"]: t for t in scan["targets"]}
        self.assertEqual({origin: (t["pages_fetched"], t["exhausted"], t["status"]) for origin, t in targets.items()}, {
            # Brightvolt and Halcyon Motors have one page of active ads (the provider sends an empty cursor).
            f"page:{acme}": (3, False, "completed"), f"page:{brightvolt}": (1, True, "completed"),
            f"page:{halcyon}": (1, True, "completed"),
            "keyword:NACS adapter": (2, False, "completed"), "keyword:CCS1 adapter": (2, False, "completed"),
        })
        groups = _fixture_ads()
        page_ads = groups[ACME] | groups[BRIGHTVOLT] | groups[HALCYON]
        keyword_ads = groups["NACS adapter"] | groups["CCS1 adapter"]
        self.assertEqual(scan["observation_count"], len(page_ads | keyword_ads))
        self.assertEqual(scan["observation_count"], 128)
        self.assertEqual(scan["cost"]["market_data"], {"calls": 9, "credits_charged": 9, "usd": "0",
                                                       "simulated_usd": "0.01692"})
        self.assertEqual(scan["media"]["pending"], 0)
        self.assertEqual(scan["media"]["available"], scan["media"]["planned"])

        ads = self._all_ads(accepted["scan_id"])
        ids = [ad["ad_archive_id"] for ad in ads]
        self.assertEqual(len(ids), len(set(ids)))  # one row per ad_archive_id
        expected_rows = sum(len(groups[key]) for key in (ACME, BRIGHTVOLT, HALCYON, *KEYWORDS))
        self.assertEqual(len(self._origins(accepted["scan_id"])), expected_rows)
        by_id = {ad["ad_archive_id"]: ad for ad in ads}
        overlap = page_ads & keyword_ads
        self.assertEqual(len(overlap), 16)
        for ad_archive_id in overlap:
            kinds = [origin["kind"] for origin in by_id[ad_archive_id]["origins"]]
            self.assertIn("page", kinds)
            self.assertIn("keyword", kinds)
            self.assertGreaterEqual(len(kinds), 2)
        for ad_archive_id in groups[ACME]:
            self.assertEqual(by_id[ad_archive_id]["competitor_ids"], [acme])
        for ad_archive_id in keyword_ads - page_ads:
            self.assertEqual((by_id[ad_archive_id]["competitor_ids"], by_id[ad_archive_id]["role"]), ([], None))
        sample = by_id[next(iter(groups[ACME]))]
        self.assertEqual(sample["media_status"], "available")
        self.assertEqual(sample["thumbnail"]["mime_type"], "image/jpeg")
        self.assertTrue(sample["thumbnail"]["url"])
        media = [card["image"] or card["video_poster"] for card in sample["cards"] if card["image"] or card["video_poster"]]
        self.assertTrue(media and all(item["status"] == "available" and item["sha256"] for item in media))
        self.assertTrue(all("?" not in (item["source_url"] or "") for item in media))  # no signature kept
        self.assertTrue(all(item["expires_at"] for item in media))

        # Filters: a competitor, origin kind, days running, format.
        self.assertEqual({ad["ad_archive_id"] for ad in self._all_ads(accepted["scan_id"], competitor_id=acme)},
                         groups[ACME])
        self.assertEqual({ad["ad_archive_id"] for ad in self._all_ads(accepted["scan_id"], origin_kind="keyword")},
                         keyword_ads)
        long_running = self._all_ads(accepted["scan_id"], min_days_running=60)
        self.assertTrue(all(ad["days_running"] >= 60 for ad in long_running))
        days = [ad["days_running"] or -1 for ad in ads]
        self.assertEqual(days, sorted(days, reverse=True))
        videos = self._all_ads(accepted["scan_id"], display_format="video")
        self.assertTrue(videos and all(ad["display_format"] == "VIDEO" for ad in videos))
        self._problem(self.client.get(f"/api/v1/competitor-scans/{accepted['scan_id']}/ads",
                                      params={"cursor": "bm90LWEtY3Vyc29y"}), 400, "invalid_cursor")

        listed = self.client.get("/api/v1/competitor-scans", params={"product_id": self.product_id}).json()["items"]
        self.assertEqual([(item["scan_id"], item["observation_count"]) for item in listed],
                         [(accepted["scan_id"], 128)])

    def test_observations_and_origins_are_append_only(self):
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        ad = self._all_ads(accepted["scan_id"])[0]
        for statement in (
            update(CompetitorAdRecord).where(CompetitorAdRecord.id == ad["ad_id"]).values(advertiser_name="x"),
            CompetitorAdRecord.__table__.delete().where(CompetitorAdRecord.id == ad["ad_id"]),
            update(CompetitorAdOriginRecord).where(CompetitorAdOriginRecord.competitor_ad_id == ad["ad_id"])
            .values(origin="keyword:x"),
            CompetitorAdOriginRecord.__table__.delete().where(CompetitorAdOriginRecord.competitor_ad_id == ad["ad_id"]),
        ):
            with self.assertRaises(DatabaseError):
                with tenant_session(self.ctx) as session:
                    session.execute(statement)

    def test_create_replays_by_key_never_by_date(self):
        body = self._body([self._confirmed(BRIGHTVOLT)], keywords=[])
        first = self._start(body, key="scan-key")
        again = self._start(body, key="scan-key")
        self.assertEqual((again["scan_id"], again["replayed"]), (first["scan_id"], True))
        self._problem(self.client.post("/api/v1/competitor-scans", json={**body, "keywords": ["NACS adapter"]},
                                       headers=self._headers("scan-key")), 409, "idempotency_key_reused")
        fresh = self._start(body)
        self.assertNotEqual(fresh["scan_id"], first["scan_id"])

    def test_the_products_own_page_is_scanned_as_self(self):
        own = self._confirmed(HALCYON, role="self")
        accepted = self._start(self._body([own], keywords=[]))
        self._drain()
        mine = self._all_ads(accepted["scan_id"], role="self")
        self.assertEqual(len(mine), 20)
        self.assertEqual(self._all_ads(accepted["scan_id"], role="competitor"), [])
        self.assertEqual(self._scan(accepted["scan_id"])["targets"][0]["role"], "self")


class ByokScanTests(ScanFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def test_scenario_3_two_lines_reserved_under_one_operation_and_settled(self):
        competitors = self._three()
        accepted = self._start(self._body())
        job_id, operation_id = accepted["job_id"], accepted["operation_id"]
        quote = accepted["quote"]
        self.assertEqual((quote["execution_mode"], quote["market_data"]["max_credits"]), ("byok", 23))
        market, llm = self._spec_call_names(competitors, KEYWORDS)
        reservations = self._scan_reservations(job_id)
        expected = ({market_data_call_key(job_id, name) for name in market}
                    | {llm_call_key(job_id, name) for name in llm})
        self.assertEqual(set(reservations), expected)  # the call names, verbatim
        self.assertEqual({row.operation_id for row in reservations.values()}, {operation_id})
        self.assertEqual({row.state for row in reservations.values()}, {"reserved"})

        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "completed", scan["failure"])
        self.assertEqual(scan["cost"]["market_data"], {"calls": 9, "credits_charged": 9, "usd": "0.01692",
                                                       "simulated_usd": None})
        # The relevance batches (C-22), one tag call per observation of the tag table and the scan's
        # one clustering (C-23; every product has the expression catalog's factor, #329) and one
        # discover call per advertiser of its table (C-33).
        evidence = self._record(accepted["scan_id"]).evidence_json
        batches = (list(evidence["filter"]["assignments"]) + list(evidence["tag"]["assignments"]) + ["cluster:1"]
                   + list(evidence["discover"]["assignments"]))
        self.assertTrue(evidence["tag"]["assignments"])
        self.assertEqual(scan["cost"]["llm"]["calls"], len(batches))
        self.assertGreater(float(scan["cost"]["llm"]["usd"]), 0)
        reservations = self._scan_reservations(job_id)
        states = {key: row.state for key, row in reservations.items()}
        self.assertNotIn("reserved", states.values())  # nothing left open
        pages_called = {f"page:{competitors[0]}:{n}" for n in (1, 2, 3)} | {
            f"page:{competitors[1]}:1", f"page:{competitors[2]}:1"} | {
            f"keyword:{k}:{n}" for k in (1, 2) for n in (1, 2)}
        self.assertEqual({key for key, state in states.items() if state == "settled"},
                         {market_data_call_key(job_id, name) for name in pages_called}
                         | {llm_call_key(job_id, name) for name in batches})
        settled = sum(row.maximum_budget_amount for row in reservations.values() if row.state == "settled")
        self.assertLessEqual(float(scan["cost"]["usd"]), float(settled))
        record = self._record(accepted["scan_id"])
        for target in record.evidence_json["targets"]:
            self.assertTrue(all(page["credits_charged"] == 1 for page in target["pages"]))
        with tenant_session(self.ctx) as session:
            attempts = session.scalars(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.job_id == job_id)).all()
            self.assertEqual({(a.step_kind, a.chargeability, a.credential_version_id) for a in attempts
                              if a.step_kind == "market_data"},
                             {("market_data", "charged", self.credential_version_id)})
            self.assertEqual({(a.step_kind, a.chargeability) for a in attempts if a.step_kind != "market_data"},
                             {("llm_chat", "charged")})
        self.assertEqual(record.initial_credential_version_id, self.credential_version_id)

    def test_scenario_4_a_402_mid_scan_is_partial_and_resumes_after_a_top_up(self):
        self.mock_credits = 4  # Acme Charge's three pages and Brightvolt's one; Halcyon Motors' first page gets a 402
        accepted = self._start(self._body())
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "partial")
        self.assertEqual(scan["failure"], {"code": "market_data_insufficient_credits", "retryable": False,
                                           "params": {"provider_code": "insufficient_credits"}})
        self.assertEqual(scan["observation_count"], 62)  # what was fetched stays visible
        self.assertTrue(scan["can_resume"])
        self.assertNotIn("reserved", {row.state for row in self._scan_reservations(accepted["job_id"]).values()})
        quote = self._resume_quote(accepted["scan_id"])
        self.assertEqual(quote["kind"], "resume")
        self.assertEqual([(line["operation"], line["calls"]) for line in quote["market_data"]["lines"]],
                         [("page", 3), ("keyword_page", 4), ("ad_refresh", 10)])

        self.mock_credits = 100  # topped up
        resumed = self._resume(accepted["scan_id"], key="resume-1")
        self.assertEqual((resumed["scan_id"], resumed["status"]), (accepted["scan_id"], "queued"))
        self.assertNotEqual(resumed["job_id"], accepted["job_id"])
        again = self._resume(accepted["scan_id"], key="resume-1")
        self.assertEqual((again["job_id"], again["replayed"]), (resumed["job_id"], True))
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual((scan["status"], scan["observation_count"], len(scan["resumes"])), ("completed", 128, 1))
        self.assertEqual(scan["cost"]["market_data"]["credits_charged"], 9)
        self.assertNotIn("reserved", {row.state for row in self._scan_reservations(resumed["job_id"]).values()})
        ids = [ad["ad_archive_id"] for ad in self._all_ads(accepted["scan_id"])]
        self.assertEqual(len(ids), len(set(ids)))
        self._problem(self.client.post(f"/api/v1/competitor-scans/{accepted['scan_id']}/resume",
                                       headers=self._headers()), 409, "competitor_scan_not_resumable")

    def test_scenario_13_a_failed_competitor_resumes_with_a_new_key(self):
        acme, brightvolt, halcyon = self._three()

        def throttle_acme(source):
            source.fail_next(429, page_id=ACME, times=3)  # first try and both retries
            return source

        self.wrap = throttle_acme
        accepted = self._start(self._body())
        self._drain()
        self.wrap = None
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "partial")
        failed = {t["origin"]: t for t in scan["targets"]}[f"page:{acme}"]
        self.assertEqual((failed["status"], failed["failure"]["code"]), ("failed", "market_data_rate_limited"))
        groups = _fixture_ads()
        acme_by_keyword = groups[ACME] & (groups["NACS adapter"] | groups["CCS1 adapter"])
        self.assertEqual(len(acme_by_keyword), 12)
        before = {ad["ad_archive_id"]: ad for ad in self._all_ads(accepted["scan_id"])}
        for ad_archive_id in acme_by_keyword:  # landed through keywords first
            self.assertEqual(before[ad_archive_id]["competitor_ids"], [])
        rows_before = {row.id for row in self._origins(accepted["scan_id"])}

        new_version = self._rotate_market_data()
        resumed = self._resume(accepted["scan_id"])
        self.assertEqual(resumed["resumes"][0]["max_credits"], 3 + 10)  # Acme Charge's pages + the refresh quota
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual((scan["status"], scan["scan_id"]), ("completed", accepted["scan_id"]))
        self.assertEqual(scan["resumes"][0]["credential_version_id"], new_version)
        self.assertEqual(scan["initial_credential_version_id"], self.credential_version_id)
        after = {ad["ad_archive_id"]: ad for ad in self._all_ads(accepted["scan_id"])}
        self.assertEqual(len(after), 128)
        for ad_archive_id in acme_by_keyword:
            self.assertEqual(after[ad_archive_id]["ad_id"], before[ad_archive_id]["ad_id"])  # same row
            self.assertEqual(after[ad_archive_id]["competitor_ids"], [acme])
        appended = [row for row in self._origins(accepted["scan_id"]) if row.id not in rows_before]
        self.assertEqual({row.origin for row in appended}, {f"page:{acme}"})
        self.assertEqual(len(appended), len(groups[ACME]))
        with tenant_session(self.ctx) as session:
            versions = set(session.scalars(select(ProviderCallAttemptRecord.credential_version_id).where(
                ProviderCallAttemptRecord.job_id == resumed["job_id"],
                ProviderCallAttemptRecord.step_kind == "market_data")).all())
        self.assertEqual(versions, {new_version})
        record = self._record(accepted["scan_id"])
        self.assertEqual(record.initial_credential_version_id, self.credential_version_id)

    def test_scenario_6_expired_media_is_refreshed_then_out_of_quota(self):
        past = datetime.now(timezone.utc) - timedelta(hours=200)

        def stale_links(source):
            # Listing pages hand out links that expired long ago; /ad hands out fresh ones.
            original = source._send

            async def send(request, *, credential):
                source._clock = (lambda: datetime.now(timezone.utc)) if request.method is SourceMethod.AD_DETAIL \
                    else (lambda: past)
                return await original(request, credential=credential)

            source._send = send
            return source

        self.wrap = stale_links
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[], max_media_refreshes=1))
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "completed", scan["failure"])
        self.assertEqual(scan["media"]["refreshes_used"], 1)
        ads = self._all_ads(accepted["scan_id"])
        refreshed = [ad for ad in ads if any((c["image"] or c["video_poster"] or {}).get("refreshed")
                                             and (c["image"] or c["video_poster"])["status"] == "available"
                                             for c in ad["cards"])]
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["media_status"], "available")
        unavailable = [ad for ad in ads if ad["media_status"] == "unavailable"]
        self.assertEqual(len(unavailable), 11)
        reasons = {(c["image"] or c["video_poster"])["reason"] for ad in unavailable for c in ad["cards"]
                   if c["image"] or c["video_poster"]}
        self.assertEqual(reasons, {"refresh_quota_exhausted"})
        self.assertTrue(all(ad["thumbnail"] is None and ad["library_url"] for ad in unavailable))
        self.assertEqual(scan["cost"]["market_data"]["credits_charged"], 2)  # its one page + one refresh
        self.assertEqual([s["status"] for s in scan["steps"]][:2], ["completed", "partial"])


class RetryEvidenceTests(ScanFixtures):
    """429 and 5xx are retried inside one call; every try is evidence (maintainer decision on #270)."""

    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def test_each_try_of_a_retried_call_is_recorded(self):
        def flaky(source):
            source.fail_next(429, page_id=BRIGHTVOLT, times=1)
            source.fail_next(500, page_id=HALCYON, times=1)  # answered, charge not reported
            return source

        self.wrap = flaky
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT), self._confirmed(HALCYON)], keywords=[]))
        self._drain()
        self.wrap = None
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "completed", scan["failure"])
        self.assertEqual(scan["cost"]["market_data"]["credits_charged"], 2)  # one charged answer per page
        record = self._record(accepted["scan_id"])
        tries = {target["page_id"]: target["pages"][0]["tries"] for target in record.evidence_json["targets"]}

        def shape(items):
            return [(item["try"], item["http_status"], item["error_code"], item["credits_charged"],
                     item["dispatch_certainty"]) for item in items]

        self.assertEqual(shape(tries[BRIGHTVOLT]), [(1, 429, "rate_limited", None, "not_dispatched"),
                                                (2, 200, None, 1, "dispatched")])
        self.assertEqual(shape(tries[HALCYON]), [(1, 500, "http_500", None, "uncertain"),
                                               (2, 200, None, 1, "dispatched")])
        for item in tries[BRIGHTVOLT] + tries[HALCYON]:
            self.assertIsInstance(item["latency_ms"], int)
            self.assertGreaterEqual(item["latency_ms"], 0)
        # The attempt's own usage report carries the tries, for later reconciliation windows.
        from app.market_data.calls import call_usage

        attempt_id = record.evidence_json["targets"][1]["pages"][0]["attempt_id"]
        with tenant_session(self.ctx) as session:
            usage = call_usage(session, self.ctx, attempt_id)
            self.assertEqual(session.get(ProviderCallAttemptRecord, attempt_id).dispatch_state, "succeeded")
        self.assertEqual(shape(usage["evidence"]["tries"]), shape(tries[HALCYON]))

    def test_a_call_that_fails_after_an_unreported_5xx_is_uncertain(self):
        def flaky(source):
            source.fail_next(500, page_id=ACME, times=1)
            source.fail_next(429, page_id=ACME, times=2)
            return source

        self.wrap = flaky
        accepted = self._start(self._body([self._confirmed(ACME)], keywords=[]))
        self._drain()
        self.wrap = None
        scan = self._scan(accepted["scan_id"])
        # The last answer was an unbilled 429, but the first try may have been charged.
        self.assertEqual((scan["status"], scan["failure"]["code"]), ("uncertain", "market_data_outcome_unknown"))
        [entry] = scan["reconciliation"]
        self.assertEqual(entry["status"], "pending")
        page = self._record(accepted["scan_id"]).evidence_json["targets"][0]["pages"][0]
        self.assertEqual([(item["try"], item["http_status"]) for item in page["tries"]], [(1, 500), (2, 429), (3, 429)])
        lost = self._scan_reservations(accepted["job_id"])[market_data_call_key(accepted["job_id"], entry["call_name"])]
        self.assertEqual(lost.state, "reserved")  # kept for reconciliation, not released as unbilled


class UncertainScanTests(ScanFixtures):
    """Scenario 5: a page lost after the dispatch fence, then the automatic reconciliation."""

    def setUp(self) -> None:
        super().setUp()
        self._bind_both()

    def _kill_on(self, page_id: str, *, cursor_present: bool = True, cached_before: bool = False):
        def wrap(source):
            original = source._send

            async def send(request, *, credential):
                if request.page_id == page_id and (request.cursor is not None) == cursor_present:
                    raise Killed("worker killed after the fence")
                response = await original(request, credential=credential)
                if cached_before and request.page_id == page_id:
                    response = dataclasses.replace(response, cached=True)
                return response

            source._send = send
            return source

        return wrap

    def _uncertain_scan(self, *, cached_before: bool = False) -> dict:
        """Acme Charge page 1 succeeds (99 credits left), page 2 dies after the fence."""
        self.wrap = self._kill_on(ACME, cached_before=cached_before)
        accepted = self._start(self._body([self._confirmed(ACME)], keywords=[]))
        runtime = self._runtime()
        with self.assertRaises(Killed):
            for _ in range(5):
                asyncio.run(runtime.run_once())
        self.wrap = None
        # The dead Worker's lease lapses; the queue's lease recovery redelivers the Job.
        expire_lease(self.ctx, accepted["job_id"])
        self._drain()
        scan = self._scan(accepted["scan_id"])
        self.assertEqual(scan["status"], "uncertain", scan)
        self.assertEqual(scan["failure"]["code"], "market_data_outcome_unknown")
        [entry] = scan["reconciliation"]
        self.assertEqual((entry["status"], entry["page"], entry["origin"]), ("pending", 2, scan["targets"][0]["origin"]))
        record = self._record(accepted["scan_id"])
        pages = record.evidence_json["targets"][0]["pages"]
        self.assertEqual([page["status"] for page in pages], ["succeeded", "uncertain"])
        open_rows = [row for row in self._scan_reservations(accepted["job_id"]).values() if row.state == "reserved"]
        self.assertEqual(len(open_rows), 1)  # the lost page's own reservation, kept for reconciliation
        with tenant_session(self.ctx) as session:
            attempt = session.get(ProviderCallAttemptRecord, entry["attempt_id"])
            self.assertEqual(attempt.dispatch_state, "uncertain")
        self.assertEqual(scan["observation_count"], 30)
        return {**scan, "accepted": accepted, "attempt_id": entry["attempt_id"]}

    def _resume_with_balance(self, scan: dict, balance: int) -> dict:
        self.balance = balance
        resumed = self._resume(scan["scan_id"])
        self._drain()
        return {**self._scan(scan["scan_id"]), "resumed": resumed}

    def test_difference_0_settles_not_charged_and_refetches_the_page(self):
        scan = self._uncertain_scan()
        done = self._resume_with_balance(scan, 99)
        [entry] = done["reconciliation"]
        self.assertEqual((entry["status"], entry["source"], entry["balance_check"]["difference"]),
                         ("not_charged", "auto", 0))
        self.assertEqual(done["status"], "completed", done["failure"])
        self.assertEqual(done["observation_count"], 50)  # page 2 fetched again; nothing doubled
        reservations = self._scan_reservations(scan["accepted"]["job_id"])
        self.assertNotIn("reserved", {row.state for row in reservations.values()})
        lost = reservations[market_data_call_key(scan["accepted"]["job_id"], f"page:{scan['targets'][0]['competitor_id']}:2")]
        self.assertEqual(lost.state, "released")
        self.assertEqual(done["cost"]["market_data"]["credits_charged"], 3)

    def test_difference_1_settles_charged(self):
        scan = self._uncertain_scan()
        done = self._resume_with_balance(scan, 98)
        [entry] = done["reconciliation"]
        self.assertEqual((entry["status"], entry["balance_check"]["difference"]), ("charged", 1))
        lost = self._scan_reservations(scan["accepted"]["job_id"])[
            market_data_call_key(scan["accepted"]["job_id"], f"page:{scan['targets'][0]['competitor_id']}:2")]
        self.assertEqual(lost.state, "settled")
        self.assertEqual(done["cost"]["market_data"]["credits_charged"], 4)  # the lost page was paid for
        self.assertEqual(done["status"], "completed")

    def test_other_differences_and_a_cached_baseline_wait_for_the_owner(self):
        for balance, cached, reason in ((97, False, "difference"), (100, False, "difference"),
                                        (99, True, "cached_baseline")):
            with self.subTest(balance=balance, cached=cached):
                self._three_ids = None
                scan = self._uncertain_scan(cached_before=cached)
                stuck = self._resume_with_balance(scan, balance)
                [entry] = stuck["reconciliation"]
                self.assertEqual((stuck["status"], entry["status"], entry["balance_check"]["outcome"],
                                  entry["balance_check"]["reason"]), ("uncertain", "pending", "manual_required", reason))
                self.assertEqual(stuck["observation_count"], 30)  # nothing else was fetched
                self.assertNotIn("reserved", {
                    row.state for row in self._scan_reservations(stuck["resumed"]["job_id"]).values()})
                # A Member may not decide; the Owner marks it and a Job settles it.
                self._problem(self._reconcile(scan["scan_id"], scan["attempt_id"], "charged"), 403,
                              "competitor_scan_reconcile_forbidden")
                self.current = self.owner_ctx(self.ctx)
                marked = self._reconcile(scan["scan_id"], scan["attempt_id"], "charged", key=f"mark-{balance}")
                self.assertEqual(marked.status_code, 200, marked.text)
                self.assertEqual(marked.json()["status"], "partial")
                replay = self._reconcile(scan["scan_id"], scan["attempt_id"], "charged", key=f"mark-{balance}")
                self.assertTrue(replay.json()["replayed"])
                self._problem(self._reconcile(scan["scan_id"], scan["attempt_id"], "not_charged"), 409,
                              "competitor_scan_reconcile_not_applicable")
                self.current = self.ctx
                self._drain()
                lost = self._scan_reservations(scan["accepted"]["job_id"])[market_data_call_key(
                    scan["accepted"]["job_id"], f"page:{scan['targets'][0]['competitor_id']}:2")]
                self.assertEqual(lost.state, "settled")
                done = self._resume_with_balance(scan, 0)
                self.assertEqual(done["status"], "completed", done["failure"])

    def test_a_resolve_between_the_lost_page_and_the_check_is_counted(self):
        scan = self._uncertain_scan()
        # The uncertain scan no longer holds the key: a resolve runs and pays 1 + 3 credits.
        resolve = self._resolve(self._competitor("Acme Charge", website="acme-charge.example")["competitor_id"])
        self._drain()
        with tenant_session(self.ctx) as session:
            resolution = session.scalars(select(CompetitorResolutionRecord).where(
                CompetitorResolutionRecord.job_id == resolve["resolve_job_id"])).one()
            self.assertEqual((resolution.status, resolution.cost_json["credits_charged"]), ("completed", 4))
        done = self._resume_with_balance(scan, 99 - 4)
        [entry] = done["reconciliation"]
        self.assertEqual((entry["status"], entry["balance_check"]["known_charges"],
                          entry["balance_check"]["difference"]), ("not_charged", 4, 0))

    def test_an_uncertain_resolve_in_the_window_leaves_the_page_to_the_owner(self):
        scan = self._uncertain_scan()

        def lose_the_search(source):
            source.fail_next(500, method="search_pages", times=3)
            return source

        self.wrap = lose_the_search
        resolve = self._resolve(self._competitor("Acme Charge", website="acme-charge.example")["competitor_id"])
        self._drain()
        self.wrap = None
        with tenant_session(self.ctx) as session:
            resolution = session.scalars(select(CompetitorResolutionRecord).where(
                CompetitorResolutionRecord.job_id == resolve["resolve_job_id"])).one()
            self.assertEqual(resolution.status, "uncertain")
        stuck = self._resume_with_balance(scan, 99)
        [entry] = stuck["reconciliation"]
        self.assertEqual((stuck["status"], entry["balance_check"]["reason"]),
                         ("uncertain", "unknown_charge_in_window"))

    def test_a_success_after_an_unreported_5xx_in_the_window_leaves_the_page_to_the_owner(self):
        scan = self._uncertain_scan()

        def flaky(source):
            source.fail_next(500, page_id=BRIGHTVOLT, times=1)
            return source

        self.wrap = flaky
        other = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        self.wrap = None
        self.assertEqual(self._scan(other["scan_id"])["status"], "completed")
        # 99 left after the baseline, 1 known charge since: 98 would read "not charged" -- unless
        # the 5xx try was charged too, which nobody reported. The Owner decides.
        stuck = self._resume_with_balance(scan, 98)
        [entry] = stuck["reconciliation"]
        self.assertEqual((stuck["status"], entry["balance_check"]["reason"]),
                         ("uncertain", "unknown_charge_in_window"))

    def test_an_uncertain_resolve_before_the_scan_does_not_disturb_it(self):
        def lose_the_search(source):
            source.fail_next(500, method="search_pages", times=3)
            return source

        self.wrap = lose_the_search
        self._resolve(self._competitor("Acme Charge", website="acme-charge.example")["competitor_id"])
        self._drain()
        self.wrap = None
        scan = self._uncertain_scan()
        done = self._resume_with_balance(scan, 99)
        self.assertEqual((done["status"], done["reconciliation"][0]["status"]), ("completed", "not_charged"))

    def test_a_new_key_cannot_be_reconciled_automatically(self):
        scan = self._uncertain_scan()
        self._rotate_market_data()
        stuck = self._resume_with_balance(scan, 99)
        self.assertEqual((stuck["status"], stuck["reconciliation"][0]["balance_check"]["reason"]),
                         ("uncertain", "credential_changed"))


class ScanAccessAndTurnTests(ScanFixtures):
    def test_another_workspace_sees_nothing(self):
        body = self._body([self._confirmed(BRIGHTVOLT)], keywords=[])
        accepted = self._start(body)
        self._drain()
        stranger = self._make_context(role="member", actor="member_b")
        self.current = stranger
        scan_id = accepted["scan_id"]
        for response in (
            self.client.get(f"/api/v1/competitor-scans/{scan_id}"),
            self.client.get(f"/api/v1/competitor-scans/{scan_id}/ads"),
            self.client.get(f"/api/v1/competitor-scans/{scan_id}/resume-quote"),
            self.client.post(f"/api/v1/competitor-scans/{scan_id}/resume", headers=self._headers()),
        ):
            self._problem(response, 404, "competitor_scan_not_found")
        self.current = self.owner_ctx(stranger)
        self._problem(self._reconcile(scan_id, "pca_x", "charged"), 404, "competitor_scan_not_found")
        self.assertEqual(self.client.get("/api/v1/competitor-scans").json()["items"], [])
        self._problem(self.client.post("/api/v1/competitor-scans/preview", json=body), 404, "product_not_found")

    def test_a_completed_scan_has_nothing_to_resume_or_reconcile(self):
        accepted = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        self._drain()
        self._problem(self.client.get(f"/api/v1/competitor-scans/{accepted['scan_id']}/resume-quote"),
                      409, "competitor_scan_not_resumable")
        self.current = self.owner_ctx(self.ctx)
        self._problem(self._reconcile(accepted["scan_id"], "pca_x", "charged"), 409,
                      "competitor_scan_reconcile_not_applicable")

    def test_scenario_16_a_second_scan_and_a_resolve_wait_their_turn(self):
        self._bind_both()
        first = self._start(self._body([self._confirmed(BRIGHTVOLT)], keywords=[]))
        second = self._start(self._body([self._confirmed(HALCYON)], keywords=[]))
        resolve = self._resolve(self._competitor("Acme Charge")["competitor_id"])
        # The first scan holds the key's turn: running with this key, its Job leased elsewhere.
        with tenant_session(self.ctx) as session:
            record = session.get(CompetitorScanRecord, first["scan_id"])
            record.status = "running"
            record.evidence_json = {**record.evidence_json, "run_credential_version_id": self.credential_version_id}
            session.execute(update(JobRecord).where(JobRecord.id == first["job_id"]).values(
                state="running", lease_owner="other-worker", lease_expires_at=utcnow() + timedelta(minutes=5)))
        runtime = self._runtime()
        for _ in range(2):
            self.assertTrue(asyncio.run(runtime.run_once()))
        waiting = self._scan(second["scan_id"])
        self.assertEqual((waiting["status"], waiting["waits"]), ("queued", 1))
        with tenant_session(self.ctx) as session:
            for job_id in (second["job_id"], resolve["resolve_job_id"]):
                job = session.get(JobRecord, job_id)
                self.assertEqual((job.state, job.error_code), ("retry_wait", "market_data_turn_busy"))
            self.assertEqual(session.scalars(select(ProviderCallAttemptRecord.id).where(
                ProviderCallAttemptRecord.job_id.in_((second["job_id"], resolve["resolve_job_id"])))).all(), [])
            # The holder's lease lapses: the others take their turns.
            session.execute(update(CompetitorScanRecord).where(CompetitorScanRecord.id == first["scan_id"]).values(
                status="queued"))
            session.execute(update(JobRecord).where(JobRecord.id == first["job_id"]).values(
                state="queued", lease_owner=None, lease_expires_at=None))
            session.execute(update(JobRecord).where(JobRecord.id.in_((second["job_id"], resolve["resolve_job_id"])))
                            .values(next_attempt_at=utcnow() - timedelta(seconds=1)))
        self._drain()
        done = [self._scan(first["scan_id"]), self._scan(second["scan_id"])]
        self.assertEqual([scan["status"] for scan in done], ["completed", "completed"])
        self.assertEqual(done[1]["waits"], 1)
        self.assertEqual([scan["cost"]["market_data"]["credits_charged"] for scan in done], [1, 1])


if __name__ == "__main__":
    import unittest

    unittest.main()
