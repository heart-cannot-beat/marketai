"""The ads-first first batch (#193): read, understand, draft the ideas and make the ads under one confirmed maximum.

Everything runs through the real endpoints and the real Worker loop, with a fake web page, a fake model
(the Controlled Mock fixtures, or a fake OpenAI answer for BYOK), a fake image provider and, for BYOK
checks (#197), a fake vision model: no paid call.
"""
import asyncio
import json
from contextlib import ExitStack
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from sqlalchemy import func, select, update

from app import config
from app.adapters.blob_store_local import LocalFileBlobStore
from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import (GenerationRunRecord, JobRecord, LibraryEntryRecord,
                                            OperationRecord, ProductIntakeRecord, ProductRecord, new_id, utcnow)
from app.entitlements.cost_models import SpendReservationRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import CredentialProtector
from app.generation.models import RunItemRecord
from app.marketing import first_batch, ideas
from app.marketing.ai_qc_worker import execute_ai_qc
from app.marketing.ideas_contracts import FirstBatchDetail, FirstBatchPage, FirstBatchQuote, IdeasProjectDetail
from app.marketing.models import MarketingFirstBatchRecord, MarketingGenerationLinkRecord, MarketingProjectRecord, PlanningCallRecord
from app.marketing.planner import example_result, execute_planning
from app.platform import jobs
from app.products import intake_extraction as extraction
from app.products.intake_fetch import SafeFetcher
from app.providers.adapter import ProviderCallResult, ProviderDispatchError
from app.providers.llm_mock import register_llm_mock_fixture
from app.worker_runtime import WorkerRuntime
from tests.support.library_fixtures import png_bytes
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.test_marketing_ideas_planning import PLANNING_ENV, IdeasPlanningFixtures

LINK = "https://acmecharge.example/products/home"
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Acme Charge Home</title>
<meta property="og:type" content="product">
<script type="application/ld+json">{"@type":"Product","name":"Acme Charge Home",
"description":"Wall charger for electric cars with app scheduling and load balancing."}</script>
</head><body><h1>Acme Charge Home</h1>
<p>Schedule charging from the Acme Charge app so the car charges overnight.</p>
<p>Load balancing keeps the home circuit from tripping while the car charges.</p>
<p>IP55 enclosure for outdoor installation in a driveway or carport.</p></body></html>"""
#: The Controlled Mock ideas, kept before a test patches ``ideas.example`` with a variation of them.
EXAMPLE = ideas.example
IMAGE_PRICES = json.dumps({"openai": {config.OPENAI_IMAGE_MODEL: {"1K": "0.05"}}})
#: The image checks' deployment settings for BYOK (#197).
QC_ENV = {"MARKETAI_MARKETING_QC_PROVIDER": "openai", "MARKETAI_MARKETING_QC_MODEL": "vision-test",
          "MARKETAI_MARKETING_QC_MAX_COST_USD": "0.05"}
#: What the web proxy accepts from each endpoint; a ``Location`` it does not declare makes the whole answer a 502.
POLICY = Path(__file__).resolve().parents[1] / "contracts" / "openapi" / "marketai-domain-v1-policy.json"


def check_report(inputs, **changes):
    """A faithful answer of the fake vision model to an ideas check: every check passes, the copy is
    rendered exactly as written. ``changes`` alters one finding (by check id) or the ``rendered_text``."""
    findings = [{"check_id": key, "status": "pass", "confidence": "high",
                 "visual_evidence": "The charger and the headline are clearly visible in the driveway scene.",
                 "rationale": "Matches the ad's claim, way of telling and copy.", "suggested_fix": "",
                 "fact_ids": inputs["evidence_fact_ids"][:1] if key == "facts_supported" else []}
                for key in inputs["required_checks"]]
    answer = {"rendered_text": {field: inputs["combination"][field] for field in ("headline", "body_copy")},
              "summary": "The ad shows its claim clearly.", "findings": findings}
    for check_id, values in changes.items():
        if check_id == "rendered_text":
            answer["rendered_text"].update(values)
        else:
            next(f for f in findings if f["check_id"] == check_id).update(values)
    return answer


def card(variants=()):
    """The model's reading of the Acme Charge page (the intake's ``product_intake`` schema)."""
    def answer(request):
        if request.schema_name != extraction.SCHEMA_NAME:
            from app.providers.llm_mock import synthesize_from_schema
            return synthesize_from_schema(request.json_schema)
        return {
            "is_product": True, "name": "Acme Charge Home",
            "description": "Wall charger for electric cars with app scheduling and load balancing.",
            "model_variants": [{"label": label, "specs": [{"name": "Power", "value": label}], "appearance": [],
                                "image_ids": []} for label in variants],
            "specs": [{"name": "Enclosure", "value": "IP55"}],
            "selling_points": ["Schedule charging from the app", "Load balancing"],
            "audiences": [{"who": "Drivers with a home driveway", "why": "Charge overnight", "pain": "Public chargers queue"}],
            "visual_must_match": [{"type": "color", "description": "Matte black enclosure"}],
            "image_ranking": [{"image_id": f"img_{n}", "role": "main"} for n in range(1, len(request.images) + 1)],
        }
    return answer


def two_claims(inputs):
    """Ideas from material that supports only two buying claims: 2 claims x 2 ways = 4 ads."""
    drafted = EXAMPLE(inputs)
    drafted["angles"] = drafted["angles"][:2]
    drafted["combinations"] = [c for c in drafted["combinations"] if c["angle_id"] in ("a1", "a2")]
    return drafted


def one_claim(inputs):
    drafted = EXAMPLE(inputs)
    drafted["angles"] = drafted["angles"][:1]
    drafted["combinations"] = [c for c in drafted["combinations"] if c["angle_id"] == "a1"]
    return drafted


def repeated_mechanism(inputs):
    """A draft the application refuses: both ways of telling the first claim use the same picture."""
    drafted = EXAMPLE(inputs)
    first = drafted["angles"][0]["expressions"]
    first[1]["visual_mechanism"] = first[0]["visual_mechanism"]
    return drafted


class FakeWeb:
    def __init__(self):
        self.routes = {("acmecharge.example", "/products/home"): httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, text=PAGE)}
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        return self.routes.get((request.headers.get("host", ""), request.url.path)) or httpx.Response(404)

    async def resolve(self, host, port):
        if host != "acmecharge.example":
            raise OSError("NXDOMAIN")
        return ["93.184.216.34"]

    def fetcher(self):
        return SafeFetcher(resolver=self.resolve, transport=httpx.MockTransport(self.handler))


class FakeImages:
    """The image provider: answers at once with a picture, never a network call."""
    models = ["mock-v1", *config.OPENAI_IMAGE_MODELS]

    def __init__(self):
        self.calls = 0

    async def execute(self, request):
        self.calls += 1
        return ProviderCallResult(png_bytes(colour=(12, 34, 56 + self.calls % 100)), f"req-{self.calls}", False)


class RefusedImages(FakeImages):
    """An image provider that refuses the key for images, as OpenAI does for an organisation not verified for
    its image models: a 403 before anything is made (``credential_rejected``)."""

    async def execute(self, request):
        self.calls += 1
        raise ProviderDispatchError("not_dispatched", "OpenAI 403: organization must be verified",
                                    error_code="credential_rejected")


class FirstBatchFixtures(IdeasPlanningFixtures):
    def setUp(self):
        super().setUp()
        self.addCleanup(cancel_workspace_jobs, self.context.workspace_id)
        self.web = FakeWeb()
        self.images = FakeImages()
        self.blobs = LocalFileBlobStore(root=config.DATA_DIR / new_id("blobs"), public_base_url="http://localhost:9000",
                                        signing_secret=b"test-only")
        self.llm_client = None
        self.complete = None
        self.qc_complete = None
        self.reading(card())
        self.addCleanup(register_llm_mock_fixture, extraction.SEMANTIC, extraction._mock_product_intake)

    def reading(self, answer):
        register_llm_mock_fixture(extraction.SEMANTIC, answer)

    # -- endpoints -------------------------------------------------------------------------------

    def quote(self, mode="mock"):
        response = self.client.get(f"/api/v1/marketing/first-batch-quote?execution_mode={mode}")
        self.assertEqual(response.status_code, 200, response.text)
        return FirstBatchQuote.model_validate(response.json()).model_dump(mode="json")

    def start(self, mode="mock", key=None, quote_hash=None, expect=201, **body):
        payload = {"url": LINK, "market": "United States", "ad_language": "en-US", "execution_mode": mode,
                   "quote_hash": quote_hash or self.quote(mode)["quote_hash"], **body}
        response = self.client.post("/api/v1/marketing/first-batch", json=payload, headers=self.headers(key=key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def batch(self, batch_id, expect=200):
        response = self.client.get(f"/api/v1/marketing/first-batch/{batch_id}")
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            FirstBatchDetail.model_validate(response.json())
        return response.json()

    def without_product(self, expect=200):
        response = self.client.get("/api/v1/marketing/first-batch")
        self.assertEqual(response.status_code, expect, response.text)
        if expect == 200:
            FirstBatchPage.model_validate(response.json())
        return response.json()

    def resume(self, batch, expect=200, key=None, **body):
        current = self.batch(batch["id"])
        payload = {"quote_hash": (current["resume"] or {}).get("quote_hash", "0" * 64), **body}
        response = self.client.post(f"/api/v1/marketing/first-batch/{batch['id']}/resume", json=payload,
                                    headers=self.headers(key=key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    # -- the Worker ------------------------------------------------------------------------------

    def drain(self, rounds=80, job_types=None):
        """Run the Worker until nothing is left, with the fake page, model and image provider."""
        quarantine_foreign_jobs(self.context.workspace_id)
        runtime = WorkerRuntime(owner=f"first-batch-{uuid4().hex[:6]}")
        runtime.blob_store = self.blobs
        runtime._intake_fetcher = self.web.fetcher()
        if self.llm_client is not None:
            runtime._llm_chat_client = self.llm_client
        with ExitStack() as stack:
            stack.enter_context(patch("app.generation.paid_execution.get_provider", return_value=self.images))
            if self.complete is not None:
                complete = self.complete

                async def paid_planning(runtime_self, job):
                    jobs.mark_running(job.context, job.id, runtime_self.owner)
                    await execute_planning(job.context, str(job.payload["call_id"]), complete=complete)
                    jobs.succeed(job.context, job.id, runtime_self.owner)
                stack.enter_context(patch.object(WorkerRuntime, "_marketing_plan", paid_planning))
            if self.qc_complete is not None:
                qc_complete = self.qc_complete

                async def paid_check(runtime_self, job):
                    jobs.mark_running(job.context, job.id, runtime_self.owner)
                    await execute_ai_qc(job.context, str(job.payload["call_id"]), runtime_self.blob_store,
                                        complete=qc_complete)
                    jobs.succeed(job.context, job.id, runtime_self.owner)
                stack.enter_context(patch.object(WorkerRuntime, "_marketing_ai_qc", paid_check))
            for _ in range(rounds):
                if not asyncio.run(runtime.run_once(job_types=job_types)):
                    break

    def started_and_drained(self, mode="mock", **body):
        batch = self.start(mode, **body)
        self.drain()
        return self.batch(batch["id"])

    # -- records ---------------------------------------------------------------------------------

    def count(self, model, **filters):
        with tenant_session(self.context) as session:
            query = select(func.count()).select_from(model).where(model.workspace_id == self.context.workspace_id)
            for name, value in filters.items():
                query = query.where(getattr(model, name) == value)
            return session.scalar(query)

    def calls(self, project_id):
        with tenant_session(self.context) as session:
            rows = session.scalars(select(PlanningCallRecord).where(PlanningCallRecord.project_id == project_id)
                                   .order_by(PlanningCallRecord.created_at, PlanningCallRecord.id)).all()
            for row in rows:
                session.expunge(row)
            return rows

    def record(self, batch_id):
        with tenant_session(self.context) as session:
            row = session.get(MarketingFirstBatchRecord, batch_id)
            session.expunge(row)
            return row

    def runs(self):
        return self.count(GenerationRunRecord)

    def advance_again(self, batch_id):
        """The same advance Job running once more, as after a duplicate notification."""
        first_batch.run_advance(self.context, batch_id)

    # -- BYOK ------------------------------------------------------------------------------------

    def byok(self, planning=None):
        """An OpenAI key, a spend limit, a priced image model and image checks; every answer comes from a fake model."""
        self.paid_setup()
        for name, value in {**PLANNING_ENV, **QC_ENV}.items():
            patcher = patch.dict("os.environ", {name: value})
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(config, "GENERATION_MAX_COSTS_JSON", IMAGE_PRICES)
        patcher.start()
        self.addCleanup(patcher.stop)
        answer = card()(SimpleNamespace(images=(), schema_name=extraction.SCHEMA_NAME, json_schema={}))
        self.openai_answers = []

        def openai(request):
            self.openai_answers.append(request)
            return httpx.Response(200, json={
                "id": "chatcmpl-first-batch", "model": "gpt-5-mini-2025-08-07",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(answer)},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 300}})
        from app.entitlements.credential_protector import LocalKmsAdapter
        from app.providers.dispatch import ExternalCallCoordinator
        from app.providers.llm_chat import LlmChatClient
        from app.providers.llm_mock import MockLlmChatAdapter
        from app.providers.openai_chat import OpenAIChatAdapter
        self.llm_client = LlmChatClient(
            coordinator=ExternalCallCoordinator(tenant_session), session_factory=tenant_session,
            protector=LocalKmsAdapter.local_only(),
            adapters={"openai": OpenAIChatAdapter(base_url="https://openai.test/v1", timeout=5,
                                                  transport=httpx.MockTransport(openai)),
                      "mock": MockLlmChatAdapter()},
            blob_store=self.blobs)
        self.planning_answers = list(planning or [])

        async def complete(provider, model, credential, system, user):
            inputs = json.loads(user)
            if "expression_catalog" in inputs:  # drafting the ideas
                draft = self.planning_answers.pop(0) if self.planning_answers else EXAMPLE
                return json.dumps(draft(inputs))
            return json.dumps(example_result("understanding", inputs))
        self.complete = AsyncMock(side_effect=complete)
        self.qc_complete = AsyncMock(side_effect=lambda provider, model, credential, inputs, images: check_report(inputs))

    def reserved(self, batch):
        """Every reservation the batch's steps hold or held, by maximum."""
        with tenant_session(self.context) as session:
            return sum((r.maximum_budget_amount for r in session.scalars(select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == self.context.workspace_id)).all()), Decimal("0"))




def drafts(*answers):
    """``ideas.example`` answering each drafting of the ideas with the next of ``answers``."""
    queue = list(answers)
    return lambda inputs: (queue.pop(0) if queue else EXAMPLE)(inputs)


class FirstBatchFlowTests(FirstBatchFixtures):
    def test_advancing_reaches_done_with_six_drafts(self):
        batch = self.start()
        self.assertEqual((batch["state"], batch["project_id"], batch["product_id"]), ("reading", None, None))
        self.drain()
        done = self.batch(batch["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertFalse(done["needs_attention"])
        self.assertEqual(done["ads_planned"], 6)
        project = self.read_ideas({"id": done["project_id"]})
        IdeasProjectDetail.model_validate(project)
        self.assertEqual(len(project["ideas"]["angles"]), 3)
        self.assertEqual(len(project["ads"]), 6)
        self.assertEqual(project["pending"], [])
        self.assertEqual(project["first_batch"], {"id": batch["id"], "state": "done", "needs_attention": False,
                                                  "reason": None})
        # One generation draft and one run per ad; nothing was confirmed along the way.
        self.assertEqual(self.count(MarketingGenerationLinkRecord, project_id=done["project_id"]), 6)
        self.assertEqual(self.runs(), 6)
        self.assertTrue(all(f["confirmation"] == "unconfirmed" for f in project["understanding"]["facts"]))
        self.assertEqual([c.kind for c in self.calls(done["project_id"])], ["understanding", "ideas"])
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(OperationRecord, done["operation_id"]).state, "succeeded")

    def test_starting_sends_a_location_only_when_the_web_proxy_declares_one(self):
        # The proxy refuses an undeclared Location with a 502 (#216): the page then says the start failed
        # although the batch exists and its maximum is confirmed.
        declared = json.loads(POLICY.read_text())["start_marketing_first_batch"].get("locationShapes", {})
        response = self.client.post("/api/v1/marketing/first-batch", headers=self.headers(), json={
            "url": LINK, "market": "United States", "ad_language": "en-US", "execution_mode": "mock",
            "quote_hash": self.quote()["quote_hash"]})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual("location" in response.headers, "201" in declared)

    def test_the_reading_becomes_an_unpublished_product_draft_and_the_ideas_source(self):
        done = self.started_and_drained()
        with tenant_session(self.context) as session:
            product = session.get(ProductRecord, done["product_id"])
            entry = session.get(LibraryEntryRecord, product.product_profile_entry_id)
            self.assertIsNone(entry.current_version_id, "the product draft is not published")
            self.assertEqual((entry.display_name, product.name), ("Acme Charge Home", "Acme Charge Home"))
            from app.adapters.persistence.models import LibraryDraftRecord
            draft = session.scalar(select(LibraryDraftRecord).where(LibraryDraftRecord.entry_id == entry.id))
            self.assertEqual(draft.content_json["source_url"], LINK)
            self.assertTrue(draft.content_json["must_match"])
            self.assertFalse(any(item["confirmed"] for item in draft.content_json["must_match"]))
            row = session.get(MarketingProjectRecord, done["project_id"])
            self.assertEqual((row.flow, row.product_id, row.market, row.ad_language),
                             ("ads_first", done["product_id"], "United States", "en-US"))
            self.assertIn("Schedule charging from the Acme Charge app", row.source_text)

    def test_two_claims_make_four_ads_and_finish(self):
        with patch("app.marketing.ideas.example", side_effect=two_claims):
            done = self.started_and_drained()
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertEqual(done["ads_planned"], 4)
        project = self.read_ideas({"id": done["project_id"]})
        self.assertEqual((len(project["ideas"]["angles"]), len(project["ads"])), (2, 4))
        self.assertEqual(self.runs(), 4)

    def test_the_same_advance_twice_adds_no_call_and_no_run(self):
        batch = self.start()
        self.drain(job_types=("products.intake", "marketing.first_batch"))
        current = self.batch(batch["id"])
        self.assertEqual(current["state"], "understanding")
        calls, steps = self.calls(current["project_id"]), self.record(batch["id"]).progress_json["steps"]
        for _ in range(3):
            self.advance_again(batch["id"])
        self.assertEqual(len(self.calls(current["project_id"])), len(calls))
        self.assertEqual(self.record(batch["id"]).progress_json["steps"], steps)
        self.drain(job_types=("marketing.plan", "marketing.first_batch"))
        self.assertEqual(self.batch(batch["id"])["state"], "generating")
        runs = self.runs()
        for _ in range(3):
            self.advance_again(batch["id"])
        self.assertEqual(self.runs(), runs)
        self.drain()
        self.assertEqual(self.batch(batch["id"])["state"], "done")
        self.assertEqual(self.runs(), 6)
        self.assertEqual(len(self.calls(current["project_id"])), 2)

    def test_the_same_link_again_returns_the_same_batch(self):
        first = self.start(key="first-click")
        again = self.start(key="first-click")
        second = self.start(key="second-click")
        self.assertEqual({first["id"], again["id"], second["id"]}, {first["id"]})
        self.assertEqual((self.count(MarketingFirstBatchRecord), self.count(ProductIntakeRecord)), (1, 1))
        other = self.start(market="Canada")
        self.assertNotEqual(other["id"], first["id"], "another market is another first batch")

    def test_a_worker_restart_mid_batch_continues_where_it_stopped(self):
        batch = self.start()
        self.drain(job_types=("products.intake",))
        # A Worker takes the advance Job and dies before finishing it.
        claimed = jobs.claim_next("doomed-worker", lease_seconds=1, job_types=("marketing.first_batch",))
        self.assertIsNotNone(claimed)
        jobs.mark_running(claimed.context, claimed.id, "doomed-worker")
        with platform_session() as session:
            session.execute(update(JobRecord).where(JobRecord.id == claimed.id)
                            .values(lease_expires_at=utcnow() - timedelta(minutes=5)))
        jobs.recover_expired_jobs()
        self.drain()
        done = self.batch(batch["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertEqual([c.kind for c in self.calls(done["project_id"])], ["understanding", "ideas"])
        self.assertEqual(self.runs(), 6)
        self.assertEqual((self.count(MarketingProjectRecord), self.count(ProductRecord, name="Acme Charge Home")), (1, 1))

    def test_an_image_lost_in_a_crash_is_never_sent_again(self):
        batch = self.start()
        self.drain(job_types=("products.intake", "marketing.plan", "marketing.first_batch"))
        self.assertEqual(self.batch(batch["id"])["state"], "generating")
        # One image's call was on its way when the Worker died: its outcome is unknown.
        with tenant_session(self.context) as session:
            item = session.scalars(select(RunItemRecord).where(RunItemRecord.workspace_id == self.context.workspace_id)
                                   .order_by(RunItemRecord.created_at, RunItemRecord.id)).first()
            item.state = "running"
            lost = item.id
        self.drain()
        stopped = self.batch(batch["id"])
        self.assertEqual((stopped["state"], stopped["attention"]["reason"]), ("needs_attention", "uncertain"))
        self.assertIsNone(stopped["resume"], "an uncertain image waits for reconciliation")
        self.assertEqual((self.runs(), self.images.calls), (6, 5), "the lost image is not sent again")
        with tenant_session(self.context) as session:
            lost_item = session.get(RunItemRecord, lost)
            self.assertEqual(lost_item.state, "uncertain")
            # The run to reconcile is named, so the reading page can point at its reconciliation (#204).
            self.assertEqual(stopped["attention"]["run_ids"], [lost_item.generation_run_id])


class FirstBatchStopTests(FirstBatchFixtures):
    def test_several_models_stop_until_one_is_chosen(self):
        self.reading(card(("7 kW", "11 kW")))
        stopped = self.started_and_drained()
        self.assertEqual((stopped["state"], stopped["needs_attention"]), ("needs_attention", True))
        self.assertEqual((stopped["attention"]["reason"], stopped["attention"]["variants"]),
                         ("several_models", ["7 kW", "11 kW"]))
        self.assertTrue(stopped["attention"]["resumable"])
        self.assertEqual(stopped["resume"]["requires"], ["selected_variant"])
        self.assertIsNone(stopped["product_id"])
        self.assertEqual(self.count(MarketingProjectRecord), 0)
        refused = self.resume(stopped, expect=422, selected_variant="22 kW")
        self.assertEqual(refused["code"], "variant_selection_required")
        resumed = self.resume(stopped, selected_variant="11 kW")
        self.assertEqual((resumed["state"], resumed["selected_variant"]), ("understanding", "11 kW"))
        self.drain()
        done = self.batch(stopped["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        with tenant_session(self.context) as session:
            row = session.get(MarketingProjectRecord, done["project_id"])
            self.assertTrue(row.source_text.endswith("Chosen model: 11 kW"))

    def test_a_page_that_cannot_be_read_ends_the_batch(self):
        self.web.routes[("acmecharge.example", "/products/home")] = httpx.Response(403)
        ended = self.started_and_drained()
        self.assertEqual((ended["state"], ended["needs_attention"]), ("failed", False))
        self.assertEqual((ended["attention"]["reason"], ended["attention"]["code"]), ("page_unreadable", "access_denied"))
        self.assertFalse(ended["attention"]["resumable"])
        self.assertIsNone(ended["resume"])
        self.assertEqual(self.count(MarketingProjectRecord), 0)
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(OperationRecord, ended["operation_id"]).state, "failed")
        refused = self.resume(ended, expect=409)
        self.assertEqual(refused["code"], "first_batch_not_stopped")
        # Pasting the link again reads it again.
        self.web.routes[("acmecharge.example", "/products/home")] = httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, text=PAGE)
        self.assertNotEqual(self.start()["id"], ended["id"])

    def test_fewer_than_two_claims_stop_before_any_image(self):
        with patch("app.marketing.ideas.example", side_effect=one_claim):
            stopped = self.started_and_drained()
        self.assertEqual((stopped["state"], stopped["attention"]["reason"]), ("needs_attention", "too_few_claims"))
        self.assertEqual(stopped["resume"]["requires"], ["additional_text"])
        self.assertEqual((self.runs(), self.count(MarketingGenerationLinkRecord)), (0, 0))
        calls = self.calls(stopped["project_id"])
        self.assertEqual([c.kind for c in calls], ["understanding", "ideas"])
        self.assertEqual(calls[1].error_code, "planner_output_invalid")
        # A stop of its own: the extra drafting kept for a refused draft is not spent on it.
        self.assertFalse(self.record(stopped["id"]).progress_json["ideas_retry_used"])
        # The overview reads the stop per product.
        project = self.read_ideas({"id": stopped["project_id"]})
        self.assertEqual(project["first_batch"], {"id": stopped["id"], "state": "needs_attention",
                                                  "needs_attention": True, "reason": "too_few_claims"})
        listed = self.client.get("/api/v1/marketing/ideas").json()["items"]
        self.assertEqual([i["first_batch"]["needs_attention"] for i in listed if i["id"] == stopped["project_id"]], [True])
        # More material: the understanding and the ideas are drafted again from it.
        refused = self.resume(stopped, expect=422)
        self.assertEqual(refused["code"], "source_required")
        resumed = self.resume(stopped, additional_text="Acme Charge Home fits a 32 A home circuit.")
        self.assertEqual(resumed["state"], "understanding")
        self.drain()
        done = self.batch(stopped["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        with tenant_session(self.context) as session:
            self.assertIn("32 A home circuit", session.get(MarketingProjectRecord, done["project_id"]).source_text)

    def test_a_refused_draft_is_drafted_again_once_with_its_errors(self):
        with patch("app.marketing.ideas.example", side_effect=drafts(repeated_mechanism)):
            done = self.started_and_drained()
        self.assertEqual(done["state"], "done", done["attention"])
        calls = self.calls(done["project_id"])
        self.assertEqual([(c.kind, c.state) for c in calls],
                         [("understanding", "succeeded"), ("ideas", "failed"), ("ideas", "succeeded")])
        feedback = calls[2].input_json["rejected_plan_feedback"]
        self.assertEqual(feedback["previous_call_id"], calls[1].id)
        self.assertEqual(feedback["validation_errors"][0]["type"], "expressions_not_distinct")
        self.assertTrue(self.record(done["id"]).progress_json["ideas_retry_used"])
        self.assertEqual(self.runs(), 6)

    def test_refused_twice_stops(self):
        with patch("app.marketing.ideas.example", side_effect=repeated_mechanism):
            stopped = self.started_and_drained()
        self.assertEqual((stopped["state"], stopped["attention"]["reason"]), ("needs_attention", "ideas_refused"))
        calls = self.calls(stopped["project_id"])
        self.assertEqual([c.kind for c in calls], ["understanding", "ideas", "ideas"])
        self.assertTrue(all(c.error_code == "planner_output_invalid" for c in calls[1:]))
        self.assertEqual(self.runs(), 0)
        # The user may ask for one more draft.
        self.assertEqual(stopped["resume"]["requires"], [])
        self.resume(stopped)
        self.drain()
        self.assertEqual(self.batch(stopped["id"])["state"], "done")
        self.assertEqual(len(self.calls(stopped["project_id"])), 4)


class FirstBatchWayBackTests(FirstBatchFixtures):
    """A user who left the reading page before the product existed finds the batch again (#204)."""

    def test_a_batch_is_listed_until_it_has_a_product(self):
        self.reading(card(("7 kW", "11 kW")))
        batch = self.start()
        listed = self.without_product()["items"]
        self.assertEqual([(i["id"], i["state"], i["needs_attention"], i["source_url"]) for i in listed],
                         [(batch["id"], "reading", False, LINK)])
        self.drain()
        stopped = self.without_product()["items"]
        self.assertEqual([(i["id"], i["state"], i["reason"]) for i in stopped],
                         [(batch["id"], "needs_attention", "several_models")])
        self.resume(self.batch(batch["id"]), selected_variant="7 kW")
        self.drain()
        self.assertEqual(self.batch(batch["id"])["state"], "done")
        self.assertEqual(self.without_product()["items"], [], "the product lists it from now on")

    def test_an_ended_batch_is_not_listed(self):
        self.web.routes[("acmecharge.example", "/products/home")] = httpx.Response(403)
        self.assertEqual(self.started_and_drained()["state"], "failed")
        self.assertEqual(self.without_product()["items"], [])

    def test_pasted_text_has_no_link(self):
        batch = self.start(url=None, text="Acme Charge Home: a 7 kW home charger with an app.")
        self.assertEqual([(i["id"], i["source_url"]) for i in self.without_product()["items"]], [(batch["id"], None)])


class FirstBatchImageFailureTests(FirstBatchFixtures):
    def test_images_refused_for_the_key_say_why(self):
        self.images = RefusedImages()
        stopped = self.started_and_drained()
        self.assertEqual((stopped["state"], stopped["attention"]["reason"], stopped["attention"]["step"]),
                         ("needs_attention", "step_failed", "images"))
        # Every image failed the same way: the stop names it, so the page can say what to do (#204).
        self.assertEqual(stopped["attention"]["code"], "credential_rejected")
        self.assertEqual(len(stopped["attention"]["combination_ids"]), 6)
        self.assertIsNone(stopped["attention"].get("run_ids"))
        # Pages that render a run item's leading code read it exactly as before.
        from app.generation.run_queries import error_code_of
        with tenant_session(self.context) as session:
            summaries = {item.error_summary for item in session.scalars(select(RunItemRecord).where(
                RunItemRecord.workspace_id == self.context.workspace_id)).all()}
        self.assertEqual(summaries, {"provider_rejected: credential_rejected"})
        self.assertEqual({error_code_of(summary) for summary in summaries}, {"provider_rejected"})


class FirstBatchPaidTests(FirstBatchFixtures):
    """BYOK with fake answers: what is reserved, against what the user confirmed."""

    def setUp(self):
        super().setUp()
        self.byok()

    def test_the_quote_is_every_step_plus_one_more_drafting(self):
        quoted = self.quote("byok")
        self.assertTrue(quoted["available"], quoted)
        steps = {step["step"]: step for step in quoted["steps"]}
        reading = first_batch._reading_maximum()
        self.assertEqual(Decimal(steps["reading"]["maximum_cost"]), reading)
        self.assertEqual([Decimal(steps[s]["maximum_cost"]) for s in ("understanding", "ideas", "ideas_retry")],
                         [Decimal("0.1")] * 3)
        self.assertEqual((steps["images"]["quantity"], Decimal(steps["images"]["unit_maximum"])), (6, Decimal("0.05")))
        # Each image's check is in the confirmed total too (#197).
        self.assertEqual((steps["checks"]["quantity"], Decimal(steps["checks"]["unit_maximum"])), (6, Decimal("0.05")))
        self.assertEqual(Decimal(quoted["maximum_cost"]), reading + Decimal("0.3") + Decimal("0.3") + Decimal("0.3"))
        self.assertEqual((quoted["image_provider"], quoted["image_model"]), ("openai", config.OPENAI_IMAGE_MODEL))

    def test_a_retried_draft_finishes_within_the_confirmed_total(self):
        self.planning_answers = [repeated_mechanism, EXAMPLE]
        batch = self.start("byok")
        self.drain()
        done = self.batch(batch["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertEqual([c.kind for c in self.calls(done["project_id"])], ["understanding", "ideas", "ideas"])
        self.assertEqual(Decimal(done["reserved_total"]), self.reserved(done))
        self.assertLessEqual(Decimal(done["reserved_total"]), Decimal(done["confirmed_max_cost"]))
        self.assertEqual(Decimal(done["reserved_total"]), Decimal(done["confirmed_max_cost"]),
                         "the extra drafting was used, so every confirmed step was reserved once")
        self.assertEqual(self.images.calls, 6)
        with tenant_session(self.context) as session:
            states = set(session.scalars(select(SpendReservationRecord.state).where(
                SpendReservationRecord.workspace_id == self.context.workspace_id)).all())
        self.assertEqual(states, {"settled"})

    def test_an_unused_retry_is_never_reserved(self):
        batch = self.start("byok")
        self.drain()
        done = self.batch(batch["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertEqual(Decimal(done["reserved_total"]), self.reserved(done))
        self.assertEqual(Decimal(done["confirmed_max_cost"]) - Decimal(done["reserved_total"]), Decimal("0.1"))

    def test_the_same_advance_twice_adds_no_reservation(self):
        batch = self.start("byok")
        self.drain(job_types=("products.intake", "marketing.first_batch"))
        self.assertEqual(self.batch(batch["id"])["state"], "understanding")
        before = (self.count(SpendReservationRecord), self.record(batch["id"]).reserved_total)
        for _ in range(3):
            self.advance_again(batch["id"])
        self.assertEqual((self.count(SpendReservationRecord), self.record(batch["id"]).reserved_total), before)

    def test_a_step_past_the_confirmed_total_stops(self):
        batch = self.start("byok")
        self.drain(job_types=("products.intake",))
        # The planning price went up after the user confirmed the start button's maximum (which includes the
        # image checks, #197, so the new price must pass it on the first planning call).
        with patch.dict("os.environ", {"MARKETAI_PLANNING_MAX_COST_USD": "0.95"}):
            self.drain()
            stopped = self.batch(batch["id"])
            self.assertEqual((stopped["state"], stopped["attention"]["reason"], stopped["attention"]["step"]),
                             ("needs_attention", "over_budget", "understanding"))
            self.assertGreater(Decimal(stopped["attention"]["needed"]), Decimal(stopped["confirmed_max_cost"]))
            self.assertEqual(self.calls(stopped["project_id"]), [])
            self.assertEqual(self.count(SpendReservationRecord), 1, "only the reading is reserved")
            # Continuing confirms the new maximum, as the start button does.
            offer = stopped["resume"]
            self.assertGreater(Decimal(offer["maximum_cost"]), Decimal(stopped["confirmed_max_cost"]))
            stale = self.client.post(f"/api/v1/marketing/first-batch/{batch['id']}/resume",
                                     json={"quote_hash": "0" * 64}, headers=self.headers())
            self.assertEqual((stale.status_code, stale.json()["code"]), (409, "first_batch_quote_changed"))
            resumed = self.resume(stopped)
            self.assertEqual(resumed["confirmed_max_cost"], offer["maximum_cost"])
            self.assertEqual(resumed["state"], "understanding")
            self.drain()
        done = self.batch(batch["id"])
        self.assertEqual(done["state"], "done", done["attention"])
        self.assertLessEqual(Decimal(done["reserved_total"]), Decimal(done["confirmed_max_cost"]))

    def test_a_quote_the_user_did_not_see_is_refused(self):
        refused = self.start("byok", quote_hash="0" * 64, expect=409)
        self.assertEqual(refused["code"], "first_batch_quote_changed")
        self.assertEqual((self.count(MarketingFirstBatchRecord), self.count(ProductIntakeRecord)), (0, 0))


class FirstBatchKeyTests(FirstBatchFixtures):
    def spend_limit(self, limit="5"):
        """A paid entitlement and a spend limit, and no key of any provider."""
        from app.entitlements.cost_models import SpendGuardrailRecord
        from app.entitlements.models import EntitlementProjectionRecord
        with tenant_session(self.context) as session:
            session.merge(EntitlementProjectionRecord(workspace_id=self.context.workspace_id, entitlement_version=1,
                lease_sequence=1, semantic_hash="first-batch-test", issued_at=utcnow(), valid_until=utcnow() + timedelta(days=1),
                resolved_entitlements_json={"controlled_mock": True, "storage.bytes.max": 10 * 1024 ** 3}))
            session.add(SpendGuardrailRecord(workspace_id=self.context.workspace_id, budget_currency="USD",
                per_operation_limit=Decimal(limit), rolling_24h_limit=Decimal(limit), period_limit=Decimal(limit),
                outstanding_exposure_limit=Decimal(limit), created_at=utcnow(), updated_at=utcnow(), updated_by="owner"))

    def save_key(self, provider):
        with tenant_session(self.context) as session:
            admin = CredentialAdmin(CredentialProtector.local_only())
            credential = admin.create(session, self.owner_ctx(self.context), credential_key=provider,
                                      provider=provider, plaintext=f"{provider}-test-key".encode())
            admin.bind(session, self.owner_ctx(self.context), binding_scope="generation", provider=provider,
                       credential_version_id=credential.credential_version_id)

    def test_a_gemini_key_alone_asks_for_an_openai_key(self):
        self.spend_limit()
        self.save_key("gemini")
        with patch.dict("os.environ", PLANNING_ENV), patch.object(config, "GENERATION_MAX_COSTS_JSON", IMAGE_PRICES):
            quoted = self.quote("byok")
            self.assertEqual((quoted["available"], quoted["unavailable_reason"]), (False, "openai_key_required"))
            refused = self.start("byok", quote_hash=quoted["quote_hash"], expect=409)
        self.assertEqual((refused["code"], refused["params"]["reason"]), ("first_batch_unavailable", "openai_key_required"))

    def test_missing_only_the_key_the_quote_names_what_the_key_will_let_start(self):
        # The start page writes this number on "save the key and start" (design doc §5.1, #216).
        self.spend_limit()
        with patch.dict("os.environ", {**PLANNING_ENV, **QC_ENV}), patch.object(config, "GENERATION_MAX_COSTS_JSON", IMAGE_PRICES):
            quoted = self.quote("byok")
            self.assertEqual((quoted["available"], quoted["unavailable_reason"]), (False, "openai_key_required"))
            reading = first_batch._reading_maximum()
            self.assertEqual({step["step"]: Decimal(step["maximum_cost"]) for step in quoted["steps"]},
                             {"reading": reading, "understanding": Decimal("0.1"), "ideas": Decimal("0.1"),
                              "ideas_retry": Decimal("0.1"), "images": Decimal("0.3"), "checks": Decimal("0.3")})
            self.assertEqual(Decimal(quoted["maximum_cost"]), reading + Decimal("0.9"))
            # Naming the number starts nothing: without the key the start is still refused.
            refused = self.start("byok", quote_hash=quoted["quote_hash"], expect=409)
            self.assertEqual(refused["params"]["reason"], "openai_key_required")
            # With the key saved, the same number is what starting confirms.
            self.save_key("openai")
            ready = self.quote("byok")
        self.assertTrue(ready["available"], ready)
        self.assertEqual(ready["maximum_cost"], quoted["maximum_cost"])

    def test_missing_the_key_and_an_image_price_the_quote_names_no_maximum(self):
        # Saving the key would not let it start, so there is no number to write on the button.
        self.spend_limit()
        with patch.dict("os.environ", {**PLANNING_ENV, **QC_ENV}), patch.object(config, "GENERATION_MAX_COSTS_JSON", "{}"):
            quoted = self.quote("byok")
        self.assertEqual((quoted["available"], quoted["unavailable_reason"], quoted["maximum_cost"]),
                         (False, "openai_key_required", "0"))

    def test_missing_the_key_a_total_over_the_spend_limit_names_no_maximum(self):
        # The whole batch is about 0.95; a limit of 0.5 per operation would refuse it once the key is saved.
        self.spend_limit("0.5")
        with patch.dict("os.environ", {**PLANNING_ENV, **QC_ENV}), patch.object(config, "GENERATION_MAX_COSTS_JSON", IMAGE_PRICES):
            quoted = self.quote("byok")
        self.assertEqual((quoted["available"], quoted["unavailable_reason"], quoted["maximum_cost"]),
                         (False, "openai_key_required", "0"))


class FirstBatchTenantTests(FirstBatchFixtures):
    def test_another_workspace_can_neither_read_nor_continue_nor_reuse_it(self):
        self.reading(card(("7 kW", "11 kW")))
        stopped = self.started_and_drained()
        mine = self.context
        self.context = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.context.workspace_id)
        self.assertEqual(self.batch(stopped["id"], expect=404)["code"], "first_batch_not_found")
        response = self.client.post(f"/api/v1/marketing/first-batch/{stopped['id']}/resume",
                                    json={"quote_hash": stopped["resume"]["quote_hash"], "selected_variant": "7 kW"},
                                    headers=self.headers())
        self.assertEqual((response.status_code, response.json()["code"]), (404, "first_batch_not_found"))
        # The same link in another workspace is that workspace's own first batch.
        theirs = self.start()
        self.assertNotEqual(theirs["id"], stopped["id"])
        # Its Worker cannot advance this workspace's batch either.
        first_batch.run_advance(self.context, stopped["id"])
        self.context = mine
        self.assertEqual(self.batch(stopped["id"])["state"], "needs_attention")
        self.assertEqual(self.record(stopped["id"]).selected_variant, None)

    def test_another_workspace_does_not_list_it(self):
        self.reading(card(("7 kW", "11 kW")))
        stopped = self.started_and_drained()
        self.assertEqual([i["id"] for i in self.without_product()["items"]], [stopped["id"]])
        self.context = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.context.workspace_id)
        self.assertEqual(self.without_product()["items"], [])

    def test_another_workspace_sees_no_first_batch_on_its_ideas(self):
        done = self.started_and_drained()
        self.context = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.context.workspace_id)
        self.assertEqual(self.client.get(f"/api/v1/marketing/ideas/{done['project_id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/marketing/ideas").json()["items"], [])


class PlanFirstTests(FirstBatchFixtures):
    def test_plan_first_drafting_queues_no_first_batch_work(self):
        project = self.create()
        self.execute(self.plan_call(project))
        self.assertEqual(self.read(project)["state"], "understanding_draft")
        self.assertEqual(self.count(JobRecord, type=first_batch.JOB_TYPE), 0)
        self.assertNotIn("first_batch", self.read(project))


class SourceTextTests(FirstBatchFixtures):
    def intake(self, **fields):
        base = {"evidence_json": {}, "input_text": None, "card_json": card()(SimpleNamespace(
            images=(), schema_name=extraction.SCHEMA_NAME, json_schema={})), "source_url": None}
        return SimpleNamespace(**{**base, **fields})

    def test_the_page_summary_and_the_pasted_text(self):
        text = first_batch.source_text(self.intake(
            evidence_json={"fetch": {"digest_text": "Page title: Acme Charge Home"}},
            input_text="Ships with a 5 m cable."))
        self.assertEqual(text, "Page title: Acme Charge Home\n\nShips with a 5 m cable.")

    def test_images_only_use_what_the_model_read_from_them(self):
        text = first_batch.source_text(self.intake())
        self.assertTrue(text.startswith("Acme Charge Home"))
        self.assertIn("Schedule charging from the app", text)

    def test_the_pasted_text_is_kept_whole_when_too_long(self):
        pasted = "Acme Charge Home. " * 700
        text = first_batch.source_text(self.intake(evidence_json={"fetch": {"digest_text": "Page " * 2000}},
                                                   input_text=pasted), "11 kW")
        self.assertLessEqual(len(text), first_batch.SOURCE_LIMIT)
        self.assertIn(pasted.strip(), text)
        self.assertTrue(text.endswith("Chosen model: 11 kW"))

    def test_fewer_than_two_claims_are_told_apart_by_the_validation_errors(self):
        from pydantic import ValidationError
        from app.marketing.ideas_contracts import Ideas
        inputs = {"understanding": example_result("understanding", {"source_text": "Acme Charge Home"}),
                  "market": "United States", "ad_language": "en-US"}
        try:
            Ideas.model_validate(one_claim(inputs))
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_url=False, include_context=False)
        recorded = json.loads(json.dumps(errors, default=str))
        self.assertTrue(first_batch.too_few_claims(SimpleNamespace(output_json={"validation_errors": recorded})))
        self.assertFalse(first_batch.too_few_claims(SimpleNamespace(output_json={"validation_errors": [
            {"type": "expressions_not_distinct", "loc": ["angles", 0, "expressions", 1, "visual_mechanism"]}]})))
        self.assertFalse(first_batch.too_few_claims(SimpleNamespace(output_json=None)))


class FirstBatchFaultTests(FirstBatchFixtures):
    def test_a_reading_that_cannot_become_a_product_ends_the_batch(self):
        from app.products.intake_service import IntakeProblem
        with patch("app.products.intake_service.proposed_content",
                   side_effect=IntakeProblem(422, "primary_audience_out_of_range", "out of range")):
            ended = self.started_and_drained()
        self.assertEqual((ended["state"], ended["attention"]["reason"], ended["attention"]["code"]),
                         ("failed", "reading_failed", "primary_audience_out_of_range"))
        self.assertEqual((self.count(MarketingProjectRecord), self.count(LibraryEntryRecord)), (0, 0))

    def test_an_advance_failing_on_its_last_attempt_waits_for_the_user(self):
        batch = self.start()
        self.drain(job_types=("products.intake",))
        with platform_session() as session:  # the advance Job's last attempt
            session.execute(update(JobRecord).where(JobRecord.workspace_id == self.context.workspace_id,
                                                    JobRecord.type == first_batch.JOB_TYPE).values(max_attempts=1))
        with patch("app.marketing.first_batch.advance", side_effect=RuntimeError("database went away")):
            self.drain(job_types=("marketing.first_batch",))
        stopped = self.batch(batch["id"])
        self.assertEqual((stopped["state"], stopped["attention"]["reason"], stopped["attention"]["code"]),
                         ("needs_attention", "step_failed", "advance_failed"))
        self.assertEqual(stopped["attention"]["step"], "reading")
        self.resume(stopped)
        self.drain()
        self.assertEqual(self.batch(batch["id"])["state"], "done")
