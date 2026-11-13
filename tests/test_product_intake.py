"""Issue #161 -- product intake end to end: API -> Worker job -> confirm -> ``prod_``.

Real HTTP routes (``TestClient``), the real Worker (``WorkerRuntime.run_once``)
over the shared durable queue, the governed ``llm_chat`` path (controlled
mock, and BYOK against a fake OpenAI), the real reference gate and Library
publish -- only DNS, the web and the bucket are fakes.

* The three input kinds (URL, uploaded images, text) each reach ``ready``
  and confirm into a published ProductVersion whose ``prod_`` bridge a
  generation draft preview resolves.
* Multi-variant pages require ``selected_variant``; only that variant enters
  the version. AI must-match items are confirmed on confirm.
* Colour editions are variants too (Issue #192): each brings its own
  appearance and images; confirming keeps only the chosen edition's.
* Fetch failures end ``fetch_failed`` with a reason code and suggestion; a
  private address is refused and audited by category, never by address.
* An answer that cannot become a card ends ``extraction_failed`` with the
  raw output kept (mock fixture, and BYOK invalid JSON twice).
* Idempotent replay (same input within 24 h, same Idempotency-Key), key
  reuse conflicts, negative tenant (foreign intake ids and upload ids are
  404), Member and Owner may both intake and confirm.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.support.library_fixtures import LibraryFixtures, png_bytes
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.test_blob_store_railway_bucket import _FakeRailwayClient
from tests.test_library_reference_commands import _provision_finalized_intent

from app.adapters.blob_store_railway_bucket import RailwayBucketBlobStore
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AuditEventRecord,
    JobRecord,
    LibraryIdempotencyRecord,
    LibraryVersionRecord,
    OperationRecord,
    ProductIntakeRecord,
    ProductRecord,
)
from app.entitlements.cost_models import SpendReservationRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.entitlements.credential_protector import LocalKmsAdapter
from app.entitlements.models import EntitlementProjectionRecord
from app.generation.draft import create_draft
from app.generation.preview import create_preview
from app.library.contracts import ROLE_MEMBER, ROLE_OWNER
from app.main import app
from app.platform.http_identity import request_tenant_context
from app.platform.tenancy import system_context
from app.products import intake_extraction as extraction
from app.products import v1_router as products_router
from app.products.intake_fetch import SafeFetcher
from app.providers.llm_chat import LlmChatClient
from app.providers.llm_mock import MockLlmChatAdapter, register_llm_mock_fixture
from app.providers.openai_chat import OpenAIChatAdapter
from app.worker_runtime import WorkerRuntime

PUBLIC_IP = "93.184.216.34"
PROTECTOR = LocalKmsAdapter.local_only()


def _png(colour: tuple[int, int, int], size=(480, 480)) -> bytes:
    return png_bytes(size, colour)


PRODUCT_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>VoltNest Wallbox</title><meta property="og:type" content="product">
<meta property="og:image" content="https://shop.example/media/hero.png">
<script type="application/ld+json">{"@type":"Product","name":"VoltNest Wallbox",
"image":["/media/side.png"],"offers":{"price":"399.00","priceCurrency":"EUR"}}</script>
</head><body><h1>VoltNest Wallbox</h1><p>Charge your EV at home three times faster.</p>
<img src="/media/detail.png" alt="LED ring"><img src="/media/tiny.png" alt="icon">
</body></html>"""


class FakeWeb:
    """``host -> addresses`` DNS plus ``(host, path) -> response`` HTTP."""

    def __init__(self) -> None:
        self.dns: dict[str, list[str]] = {"shop.example": [PUBLIC_IP]}
        self.routes: dict[tuple[str, str], httpx.Response] = {
            ("shop.example", "/p/wallbox"): httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}, text=PRODUCT_PAGE,
            ),
            ("shop.example", "/media/hero.png"): self._image(_png((20, 20, 20))),
            ("shop.example", "/media/side.png"): self._image(_png((200, 30, 30))),
            ("shop.example", "/media/detail.png"): self._image(_png((30, 200, 30))),
            ("shop.example", "/media/tiny.png"): self._image(_png((1, 2, 3), size=(64, 64))),
        }
        self.requests: list[httpx.Request] = []

    @staticmethod
    def _image(data: bytes) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=data)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.routes.get((request.headers.get("host", ""), request.url.path)) or httpx.Response(404)

    async def resolve(self, host: str, port: int) -> list[str]:
        if host not in self.dns:
            raise OSError("NXDOMAIN")
        return list(self.dns[host])

    def fetcher(self) -> SafeFetcher:
        return SafeFetcher(resolver=self.resolve, transport=httpx.MockTransport(self.handler))


class IntakeFixtures(LibraryFixtures):
    def setUp(self) -> None:
        self.ctx = self._make_context(role=ROLE_MEMBER, actor="member_a")
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)
        self.current = self.ctx
        app.dependency_overrides[request_tenant_context] = lambda: self.current
        self.addCleanup(app.dependency_overrides.pop, request_tenant_context, None)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.bucket = _FakeRailwayClient()
        self.store = RailwayBucketBlobStore(
            bucket="marketai-test", endpoint_url="https://storage.invalid", client=self.bucket,
        )
        patcher = patch.object(products_router, "_blob_store", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.web = FakeWeb()
        self.llm_client: LlmChatClient | None = None

    # -- helpers ----------------------------------------------------------

    def _headers(self, key: str | None = None) -> dict:
        return {"Idempotency-Key": key or f"ik-{uuid.uuid4().hex}"}

    def _create(self, body: dict, *, key: str | None = None, expect: int = 202) -> dict:
        response = self.client.post("/api/v1/products/intake", json=body, headers=self._headers(key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _get(self, intake_id: str, *, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/products/intake/{intake_id}")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _confirm(self, intake_id: str, body: dict | None = None, *, key: str | None = None,
                 expect: int = 200) -> dict:
        response = self.client.post(
            f"/api/v1/products/intake/{intake_id}/confirm", json=body or {}, headers=self._headers(key),
        )
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _drain(self) -> None:
        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner=f"intake-{uuid.uuid4().hex[:6]}")
        runtime.blob_store = self.store
        runtime._intake_fetcher = self.web.fetcher()
        if self.llm_client is not None:
            runtime._llm_chat_client = self.llm_client
        for _ in range(20):
            if not asyncio.run(runtime.run_once()):
                break

    def _upload(self, ctx, data: bytes, *, purpose: str = "product_intake_image") -> str:
        key = f"committed/{ctx.workspace_id}/{purpose}/2026/10/10/{uuid.uuid4().hex}.png"
        self.bucket.put_test_object(key, data, content_type="image/png")
        intent_id, _ = _provision_finalized_intent(
            ctx, committed_key=key, committed_version=f"sha256:{hashlib.sha256(data).hexdigest()}",
            purpose=purpose, raw=data, write_file=False, content_type="image/png",
            filename="charger.png",
        )
        return intent_id

    def _record(self, intake_id: str) -> ProductIntakeRecord:
        with tenant_session(self.ctx) as session:
            record = session.get(ProductIntakeRecord, intake_id)
            session.expunge(record)
            return record

    def _audits(self, event_type: str) -> list[AuditEventRecord]:
        with tenant_session(self.ctx) as session:
            rows = session.scalars(
                select(AuditEventRecord).where(
                    AuditEventRecord.workspace_id == self.ctx.workspace_id,
                    AuditEventRecord.event_type == event_type,
                )
            ).all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    def _published(self, version_id: str) -> dict:
        with tenant_session(self.ctx) as session:
            return dict(session.get(LibraryVersionRecord, version_id).content_json)

    def _assert_preview_resolves(self, product_id: str) -> None:
        with tenant_session(self.ctx) as session:
            product = session.get(ProductRecord, product_id)
            self.assertIsNotNone(product)
            draft = create_draft(
                session, self.ctx, product_id=product_id,
                provider="mock", model="mock-v1", execution_mode="mock",
                combination_strategy={"kind": "full"},
            )
            self.assertEqual(draft.product_id, product_id)
            preview = create_preview(session, self.ctx, draft.id, draft.revision)
            self.assertEqual(preview.snapshot_json["product_id"], product_id)


class TestThreeInputKinds(IntakeFixtures):
    def test_url_intake_fetches_extracts_gates_and_confirms_into_a_bridged_product(self):
        accepted = self._create({"url": "https://shop.example/p/wallbox"})
        self.assertEqual(accepted["status"], "queued")
        self.assertFalse(accepted["replayed"])
        self.assertTrue(accepted["operation_id"])

        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "ready", intake.get("failure"))
        self.assertEqual(intake["execution_mode"], "mock")
        fetch = intake["evidence"]["fetch"]
        self.assertEqual(fetch["status"], "succeeded")
        self.assertEqual(fetch["http_status"], 200)
        self.assertEqual(len(fetch["body_sha256"]), 64)
        self.assertNotIn("digest_text", fetch, "the page digest stays server-side")
        self.assertEqual(fetch["images"]["kept"], 3)
        self.assertIn("too_small", {item["code"] for item in fetch["images"]["skipped"]})
        self.assertEqual(intake["evidence"]["extract"]["status"], "succeeded")
        self.assertEqual(intake["evidence"]["images"]["released"], 3)
        # Pinned: every request went to the checked public address.
        self.assertTrue(all(request.url.host == PUBLIC_IP for request in self.web.requests))

        card = intake["card"]
        self.assertEqual(card["name"], "VoltNest 家用交流充电桩")
        self.assertTrue(card["requires_variant_selection"])
        self.assertEqual([image["image_id"] for image in card["images"]], ["img_1", "img_2", "img_3"])
        self.assertTrue(all(image["selectable"] for image in card["images"]))
        self.assertEqual(card["default_image_ids"], ["img_1", "img_2", "img_3"])
        self.assertTrue(all(item["proposed_by"] == "ai" and not item["confirmed"] for item in card["must_match"]))
        self.assertEqual(card["source_url"], "https://shop.example/p/wallbox")
        with tenant_session(self.ctx) as session:
            operation = session.get(OperationRecord, accepted["operation_id"])
            self.assertEqual(operation.state, "succeeded")
            self.assertEqual(operation.result_resource_id, accepted["intake_id"])
            self.assertIsNone(session.scalars(
                select(SpendReservationRecord).where(
                    SpendReservationRecord.workspace_id == self.ctx.workspace_id)
            ).first(), "mock reserves nothing real")

        # A preview link for a candidate image (released derivative only).
        grant = self.client.post(
            f"/api/v1/products/intake/{accepted['intake_id']}/images/img_1/download-intent",
            headers=self._headers(),
        )
        self.assertEqual(grant.status_code, 201, grant.text)
        self.assertIn("reference_derivative", grant.json()["download_url"])

        # Confirming without choosing a variant is refused.
        refused = self._confirm(accepted["intake_id"], {}, expect=422)
        self.assertEqual(refused["code"], "variant_selection_required")

        confirmed = self._confirm(accepted["intake_id"], {
            "selected_variant": "11kW 三相",
            "image_ids": ["img_1", "img_3"],
            "card": {
                "selling_points": ["一晚充满", "App 预约谷电"],
                "primary_audience_index": 1,
                "must_match": [
                    {**{k: card["must_match"][0][k] for k in ("id", "type", "description")}},
                    {"type": "text", "description": "侧面印有 11kW 字样"},
                ],
            },
        })
        self.assertEqual(confirmed["status"], "confirmed")
        product_id = confirmed["product_id"]
        self.assertTrue(product_id.startswith("prod_"))
        confirmation = confirmed["confirmation"]
        self.assertEqual(confirmation["product_id"], product_id)
        self.assertEqual(len(confirmation["bound_images"]), 2)
        # Confirm composes Library idempotency keys from several ids (intake +
        # reference object + entry); PostgreSQL rejects anything wider than
        # the varchar(128) column, so every stored key must fit it.
        with tenant_session(self.ctx) as session:
            stored_keys = session.scalars(
                select(LibraryIdempotencyRecord.idempotency_key).where(
                    LibraryIdempotencyRecord.workspace_id == self.ctx.workspace_id,
                )
            ).all()
        self.assertTrue(stored_keys)
        self.assertLessEqual(max(len(key) for key in stored_keys), 128, stored_keys)

        content = self._published(confirmation["version_id"])
        self.assertEqual(content["selling_points"], ["一晚充满", "App 预约谷电"])
        self.assertEqual(content["audience"], "注重电费的通勤族：用谷电时段充电降低每公里成本")
        self.assertEqual(content["audiences"][0]["who"], "注重电费的通勤族")
        self.assertEqual([v["label"] for v in content["model_variants"]], ["11kW 三相"])
        self.assertEqual(content["specs"]["额定功率"], "11 kW")
        self.assertEqual(content["specs"]["接口"], "Type 2（IEC 62196-2），5 米线缆")
        self.assertEqual(content["source_url"], "https://shop.example/p/wallbox")
        self.assertNotIn("landing_page_url", content)
        self.assertEqual(
            [(item["proposed_by"], item["confirmed"]) for item in content["must_match"]],
            [("ai", True), ("user", True)],
        )
        self.assertEqual(content["must_match"][0]["id"], card["must_match"][0]["id"])
        self.assertEqual(len(content["reference_bindings"]), 2)
        self.assertEqual(content["reference_bindings"][0]["viewpoint"], "main")
        self._assert_preview_resolves(product_id)

        # Replaying the same confirmation returns it; a different one is refused.
        again = self._confirm(accepted["intake_id"], {
            "selected_variant": "11kW 三相",
            "image_ids": ["img_1", "img_3"],
            "card": {
                "selling_points": ["一晚充满", "App 预约谷电"],
                "primary_audience_index": 1,
                "must_match": [
                    {**{k: card["must_match"][0][k] for k in ("id", "type", "description")}},
                    {"type": "text", "description": "侧面印有 11kW 字样"},
                ],
            },
        })
        self.assertTrue(again["replayed"])
        self.assertEqual(again["product_id"], product_id)
        conflict = self._confirm(accepted["intake_id"], {"selected_variant": "7kW 单相"}, expect=409)
        self.assertEqual(conflict["code"], "intake_already_confirmed")
        self.assertEqual(len(self._audits("product_intake.confirmed")), 1)
        steps = {row.details_json["step"] for row in self._audits("product_intake.step")}
        self.assertEqual(steps, {"fetch", "extract", "images"})
        self.assert_stored_widths()

    def test_text_only_intake_skips_fetch(self):
        accepted = self._create({"text": "VoltNest 家用充电桩，7kW/11kW 两款，Type 2 接口，IP55。"})
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "ready", intake.get("failure"))
        self.assertEqual(intake["evidence"]["fetch"]["status"], "skipped")
        self.assertEqual(intake["card"]["images"], [])
        self.assertEqual(self.web.requests, [])
        confirmed = self._confirm(accepted["intake_id"], {"selected_variant": "7kW 单相"})
        content = self._published(confirmed["confirmation"]["version_id"])
        self.assertNotIn("source_url", content)
        self.assertEqual(content["reference_bindings"], [])
        self.assertEqual(content["specs"]["额定功率"], "7 kW")
        self._assert_preview_resolves(confirmed["product_id"])

    def test_image_only_intake_reads_uploads_and_binds_them(self):
        first = self._upload(self.ctx, _png((90, 90, 90)))
        second = self._upload(self.ctx, _png((180, 60, 10)))
        accepted = self._create({"upload_intent_ids": [first, second]})
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "ready", intake.get("failure"))
        self.assertEqual(intake["input"]["upload_count"], 2)
        self.assertEqual([image["source"] for image in intake["card"]["images"]], ["upload", "upload"])
        self.assertEqual(intake["card"]["images"][0]["width"], 480, "dimensions measured by the gate")
        self.assertEqual(intake["evidence"]["extract"]["images_sent"], 2)
        confirmed = self._confirm(accepted["intake_id"], {"selected_variant": "7kW 单相"})
        content = self._published(confirmed["confirmation"]["version_id"])
        self.assertEqual(len(content["reference_bindings"]), 2)
        bound_version = content["reference_bindings"][0]["reference_version_id"]
        rights = self._published(bound_version)["rights"]
        self.assertEqual(rights["scope"], "user_upload")
        self.assertEqual(rights["declared_by"], "member_a")
        self._assert_preview_resolves(confirmed["product_id"])
        # Confirm promotes each upload under a key composed of several ids.
        self.assert_stored_widths()

    def test_owner_can_confirm_into_an_existing_product(self):
        owner = self.owner_ctx(self.ctx, actor="owner_a")
        self.current = owner
        entry_id, _version = self._publish(owner, "product", "Old name", {
            "name": "Old name", "description": "old", "audience": "everyone", "selling_points": ["x"],
        })
        accepted = self._create({"text": "VoltNest 充电桩"})
        self._drain()
        confirmed = self._confirm(accepted["intake_id"], {"selected_variant": "7kW 单相", "entry_id": entry_id})
        self.assertEqual(confirmed["confirmation"]["entry_id"], entry_id)
        self.assertEqual(confirmed["confirmation"]["version_no"], 2)
        with tenant_session(owner) as session:
            products = session.scalars(
                select(ProductRecord).where(ProductRecord.product_profile_entry_id == entry_id)
            ).all()
            self.assertEqual(len(products), 1)
            self.assertEqual(products[0].id, confirmed["product_id"])
            self.assertEqual(products[0].name, "VoltNest 家用交流充电桩")
        self.assert_stored_widths(owner)


class TestColourVariants(IntakeFixtures):
    """Issue #192: colour editions are variants; only the chosen one is published."""

    TEXT = "VoltNest 家用充电桩，有曜石黑、冰川白、丛林迷彩三种配色。"

    def _colour_intake(self) -> tuple[str, dict]:
        uploads = [self._upload(self.ctx, _png(colour)) for colour in (
            (60, 60, 60), (10, 10, 10), (240, 240, 240), (60, 90, 40),
        )]
        accepted = self._create({"upload_intent_ids": uploads, "text": self.TEXT})
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "ready", intake.get("failure"))
        return accepted["intake_id"], intake["card"]

    def test_card_offers_each_edition_with_its_appearance_and_images(self):
        intake_id, card = self._colour_intake()
        self.assertTrue(card["requires_variant_selection"])
        variants = {variant["label"]: variant for variant in card["model_variants"]}
        self.assertEqual(list(variants), ["曜石黑", "冰川白", "丛林迷彩"])
        shared_colour = next(item for item in card["must_match"] if item["type"] == "color")
        self.assertIn("可选配色", shared_colour["description"])

        white = variants["冰川白"]
        self.assertEqual(white["specs"], {"外壳": "亮面冰川白 PC+ABS 阻燃外壳"})
        self.assertEqual([item["type"] for item in white["appearance"]], ["color"])
        self.assertEqual(
            (white["appearance"][0]["proposed_by"], white["appearance"][0]["confirmed"]), ("ai", False),
        )
        self.assertEqual(white["replaces_must_match_ids"], [shared_colour["id"]])
        self.assertEqual(white["image_ids"], ["img_3"])
        self.assertEqual(white["default_image_ids"], ["img_1", "img_3"])
        self.assertEqual(variants["曜石黑"]["default_image_ids"], ["img_1", "img_2"])
        owners = {image["image_id"]: image["variant_label"] for image in card["images"]}
        self.assertEqual(owners, {"img_1": None, "img_2": "曜石黑", "img_3": "冰川白", "img_4": "丛林迷彩"})
        # Before a variant is chosen nothing is filtered (and confirm is refused).
        self.assertEqual(card["default_image_ids"], ["img_1", "img_2", "img_3", "img_4"])
        refused = self._confirm(intake_id, {"image_ids": ["img_1"]}, expect=422)
        self.assertEqual(refused["code"], "variant_selection_required")
        unknown = self._confirm(intake_id, {"selected_variant": "Camo"}, expect=422)
        self.assertEqual(unknown["code"], "unknown_variant")
        self.assertEqual(self._record(intake_id).status, "ready")

    def test_confirm_keeps_only_the_chosen_editions_appearance_images_and_specs(self):
        intake_id, card = self._colour_intake()
        white = next(variant for variant in card["model_variants"] if variant["label"] == "冰川白")
        confirmed = self._confirm(intake_id, {"selected_variant": "冰川白"})
        confirmation = confirmed["confirmation"]
        self.assertEqual([item["image_id"] for item in confirmation["bound_images"]], ["img_1", "img_3"])

        content = self._published(confirmation["version_id"])
        self.assertEqual(content["model_variants"], [{"label": "冰川白", "specs": white["specs"]}])
        self.assertEqual(content["specs"]["外壳"], "亮面冰川白 PC+ABS 阻燃外壳")
        self.assertEqual(content["specs"]["接口"], "Type 2（IEC 62196-2），5 米线缆")
        descriptions = [item["description"] for item in content["must_match"]]
        self.assertEqual(descriptions[-1], white["appearance"][0]["description"])
        self.assertFalse(any("可选配色" in text or "曜石黑" in text or "迷彩" in text for text in descriptions))
        self.assertTrue(all(item["proposed_by"] == "ai" and item["confirmed"] for item in content["must_match"]))
        self.assertIn(white["appearance"][0]["id"], [item["id"] for item in content["must_match"]])
        self.assertEqual(len(content["reference_bindings"]), 2)

        audit_row = self._audits("product_intake.confirmed")[0]
        self.assertEqual(audit_row.details_json["selected_variant"], "冰川白")
        self.assertEqual(audit_row.details_json["bound_image_ids"], ["img_1", "img_3"])
        self._assert_preview_resolves(confirmed["product_id"])
        self.assert_stored_widths()

    def test_the_users_lists_win_and_another_editions_image_can_be_ticked_back(self):
        intake_id, card = self._colour_intake()
        camo = next(variant for variant in card["model_variants"] if variant["label"] == "丛林迷彩")
        appearance = camo["appearance"][0]
        confirmed = self._confirm(intake_id, {
            "selected_variant": "丛林迷彩",
            "image_ids": ["img_4", "img_2"],
            "card": {
                "specs": {"外壳": "迷彩外壳（用户改写）", "防护等级": "IP55"},
                "must_match": [
                    {k: appearance[k] for k in ("id", "type", "description")},
                    {"type": "text", "description": "侧面印有 Camo Edition"},
                ],
            },
        })
        content = self._published(confirmed["confirmation"]["version_id"])
        self.assertEqual(content["specs"], {"外壳": "迷彩外壳（用户改写）", "防护等级": "IP55"})
        self.assertEqual(
            [(item["id"] == appearance["id"], item["proposed_by"]) for item in content["must_match"]],
            [(True, "ai"), (False, "user")],
        )
        self.assertEqual(
            [item["image_id"] for item in confirmed["confirmation"]["bound_images"]], ["img_4", "img_2"],
        )
        self.assert_stored_widths()

    def test_other_workspace_and_non_members_cannot_confirm_an_edition(self):
        intake_id, _card = self._colour_intake()
        self.current = self._make_context(role=ROLE_OWNER, actor="intruder")
        missing = self._confirm(intake_id, {"selected_variant": "冰川白"}, expect=404)
        self.assertEqual(missing["code"], "product_intake_not_found")
        # A system identity in the right Workspace is not a member either.
        self.current = system_context(self.ctx.workspace_id, initiator_id="worker_only")
        forbidden = self._confirm(intake_id, {"selected_variant": "冰川白"}, expect=403)
        self.assertEqual(forbidden["code"], "product_intake_forbidden")
        self.current = self.ctx
        self.assertEqual(self._record(intake_id).status, "ready")
        self.assertEqual(self._audits("product_intake.confirmed"), [])


class TestFailuresAndSafety(IntakeFixtures):
    def test_blocked_page_ends_fetch_failed_with_a_suggestion(self):
        self.web.routes[("shop.example", "/p/blocked")] = httpx.Response(403, text="bot detected")
        accepted = self._create({"url": "https://shop.example/p/blocked"})
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "fetch_failed")
        failure = intake["failure"]
        self.assertEqual(failure["code"], "access_denied")
        self.assertEqual(failure["step"], "fetch")
        self.assertEqual(failure["suggested_action"], "paste_text_or_screenshot")
        self.assertIsNone(intake["card"])
        with tenant_session(self.ctx) as session:
            operation = session.get(OperationRecord, accepted["operation_id"])
            self.assertEqual(operation.state, "failed")
        # A failed intake is not replayed: pasting the same link again starts over.
        again = self._create({"url": "https://shop.example/p/blocked"})
        self.assertNotEqual(again["intake_id"], accepted["intake_id"])

    def test_transient_fetch_failure_is_retried_by_the_job(self):
        from datetime import timedelta

        from app.adapters.persistence.database import platform_session
        from app.adapters.persistence.models import utcnow

        self.web.routes[("shop.example", "/p/flaky")] = httpx.Response(503, text="busy")
        accepted = self._create({"url": "https://shop.example/p/flaky"})
        self._drain()
        record = self._record(accepted["intake_id"])
        self.assertEqual(record.status, "fetching")
        self.assertEqual(record.evidence_json["fetch"]["status"], "retrying")
        self.assertEqual(record.evidence_json["fetch"]["code"], "http_error")
        with platform_session() as session:
            job = session.get(JobRecord, record.job_id)
            self.assertEqual(job.state, "retry_wait")
            job.next_attempt_at = utcnow() - timedelta(seconds=1)

        self.web.routes[("shop.example", "/p/flaky")] = httpx.Response(
            200, headers={"content-type": "text/html"}, text=PRODUCT_PAGE,
        )
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "ready", intake.get("failure"))
        self.assertEqual(intake["evidence"]["fetch"]["attempts"], 2)
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.get(OperationRecord, accepted["operation_id"]).state, "succeeded")

    def test_private_address_is_refused_and_audited_by_category(self):
        self.web.dns["intranet.example"] = ["10.20.30.40"]
        accepted = self._create({"url": "http://intranet.example/admin"})
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "fetch_failed")
        self.assertEqual(intake["failure"]["code"], "blocked_address")
        self.assertEqual(intake["failure"]["blocked_category"], "private")
        self.assertEqual(self.web.requests, [])
        refusals = self._audits("product_intake.fetch_refused")
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0].details_json["blocked_category"], "private")
        self.assertNotIn("10.20.30.40", json.dumps([row.details_json for row in self._audits("product_intake.step")]))
        self.assertNotIn("10.20.30.40", json.dumps(refusals[0].details_json))

    def test_metadata_redirect_and_localhost_are_refused(self):
        self.web.routes[("shop.example", "/go")] = httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"},
        )
        accepted = self._create({"url": "https://shop.example/go"})
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual((intake["failure"]["code"], intake["failure"]["blocked_category"]),
                         ("blocked_address", "metadata"))
        refused = self._create({"url": "http://localhost:80/"}, expect=422)
        self.assertEqual(refused["code"], "blocked_address")
        bad_scheme = self._create({"url": "file:///etc/passwd"}, expect=422)
        self.assertEqual(bad_scheme["code"], "scheme_not_allowed")

    def test_schema_failure_keeps_the_raw_output(self):
        def unusable(request):
            answer = extraction._mock_product_intake(request)
            return {**answer, "name": "   ", "selling_points": []}

        register_llm_mock_fixture(extraction.SEMANTIC, unusable)
        self.addCleanup(register_llm_mock_fixture, extraction.SEMANTIC, extraction._mock_product_intake)
        accepted = self._create({"text": "something"})
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "extraction_failed")
        self.assertEqual(intake["failure"]["code"], "extraction_invalid")
        record = self._record(accepted["intake_id"])
        self.assertEqual(record.extraction_json["raw_output"]["name"], "   ")
        self.assertIn("name is empty", record.extraction_json["problems"])
        self.assertIsNone(record.card_json)
        self._confirm(accepted["intake_id"], {}, expect=409)

    def test_a_link_that_normalizes_past_the_column_width_is_refused(self):
        # product_intakes.source_url is varchar(2048). A 2048-character link
        # with no path normalizes to 2049 characters ("/" is added), which
        # PostgreSQL would reject with a 500; it is refused as too long instead.
        base = "https://shop.example?q="
        too_long = base + "a" * (2048 - len(base))
        self.assertEqual(len(too_long), 2048)
        self.assertEqual(self._create({"url": too_long}, expect=422)["code"], "invalid_url")

        base = "https://shop.example/p?q="
        at_limit = base + "a" * (2048 - len(base))
        accepted = self._create({"url": at_limit})
        self.assertEqual(self._record(accepted["intake_id"]).source_url, at_limit)
        self.assert_stored_widths()

    def test_input_is_required_and_bounded(self):
        self.assertEqual(self._create({}, expect=422)["code"], "intake_input_required")
        self.assertEqual(self._create({"text": "   "}, expect=422)["code"], "intake_input_required")
        response = self.client.post(
            "/api/v1/products/intake", json={"text": "x", "extra": 1}, headers=self._headers(),
        )
        self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/v1/products/intake", json={"text": "x"})
        self.assertEqual(response.status_code, 422, "Idempotency-Key is required")


class TestReplayAndTenancy(IntakeFixtures):
    def test_same_input_within_24h_returns_the_same_intake(self):
        first = self._create({"url": "https://shop.example/p/wallbox"})
        second = self._create({"url": "https://SHOP.example/p/wallbox#reviews"})
        self.assertEqual(second["intake_id"], first["intake_id"])
        self.assertTrue(second["replayed"])
        upload = self._upload(self.ctx, _png((5, 6, 7)))
        third = self._create({"upload_intent_ids": [upload], "text": "充电桩"})
        # Same bytes uploaded again under a new intent are the same upload set.
        upload_again = self._upload(self.ctx, _png((5, 6, 7)))
        fourth = self._create({"upload_intent_ids": [upload_again], "text": "充电桩"})
        self.assertEqual(fourth["intake_id"], third["intake_id"])
        with tenant_session(self.ctx) as session:
            jobs = session.scalars(
                select(JobRecord).where(
                    JobRecord.workspace_id == self.ctx.workspace_id, JobRecord.type == "products.intake",
                )
            ).all()
            self.assertEqual(len(jobs), 2, "a replay enqueues nothing")

    def test_idempotency_key_replays_and_conflicts(self):
        key = f"ik-{uuid.uuid4().hex}"
        first = self._create({"text": "充电桩 A"}, key=key)
        again = self._create({"text": "充电桩 A"}, key=key)
        self.assertEqual(again["intake_id"], first["intake_id"])
        conflict = self._create({"text": "充电桩 B"}, key=key, expect=409)
        self.assertEqual(conflict["code"], "idempotency_key_reused")

    def test_other_workspace_cannot_read_confirm_or_use_uploads(self):
        accepted = self._create({"text": "充电桩"})
        self._drain()
        other = self._make_context(role=ROLE_OWNER, actor="intruder")
        self.current = other
        missing = self._get(accepted["intake_id"], expect=404)
        self.assertEqual(missing["code"], "product_intake_not_found")
        self._confirm(accepted["intake_id"], {"selected_variant": "7kW 单相"}, expect=404)
        response = self.client.post(
            f"/api/v1/products/intake/{accepted['intake_id']}/images/img_1/download-intent",
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 404)

        foreign_upload = self._upload(self.ctx, _png((9, 9, 9)))
        refused = self._create({"upload_intent_ids": [foreign_upload]}, expect=404)
        self.assertEqual(refused["code"], "upload_intent_not_found")
        self.current = self.ctx
        self.assertEqual(self._get(accepted["intake_id"])["status"], "ready")

    def test_upload_purpose_and_state_are_checked(self):
        wrong_purpose = self._upload(self.ctx, _png((1, 1, 1)), purpose="library_manual_fact_import")
        refused = self._create({"upload_intent_ids": [wrong_purpose]}, expect=422)
        self.assertEqual(refused["code"], "upload_purpose_not_allowed")


class TestByok(IntakeFixtures):
    def setUp(self) -> None:
        super().setUp()
        with tenant_session(self.ctx) as session:
            session.merge(EntitlementProjectionRecord(
                workspace_id=self.ctx.workspace_id,
                entitlement_version=1, lease_sequence=1, projection_version=1,
                semantic_hash="hash_intake", resolved_entitlements_json={"features": ["generation"], "storage.bytes.max": 10 * 1024 ** 3},
            ))
        owner = self.owner_ctx(self.ctx)
        admin = CredentialAdmin(PROTECTOR)
        with tenant_session(owner) as session:
            meta = admin.create(
                session, owner, credential_key="OPENAI_API_KEY", provider="openai", plaintext=b"sk-ws-own",
            )
            admin.bind(
                session, owner, binding_scope="generation", provider="openai",
                credential_version_id=meta.credential_version_id,
            )
        self.openai_requests: list[httpx.Request] = []
        self.openai_answers: list[httpx.Response] = []

    def _openai(self, request: httpx.Request) -> httpx.Response:
        self.openai_requests.append(request)
        return self.openai_answers.pop(0)

    def _use_fake_openai(self, *answers: httpx.Response) -> None:
        from app.providers.dispatch import ExternalCallCoordinator

        self.openai_answers = list(answers)
        self.llm_client = LlmChatClient(
            coordinator=ExternalCallCoordinator(tenant_session),
            session_factory=tenant_session,
            protector=PROTECTOR,
            adapters={
                "openai": OpenAIChatAdapter(
                    base_url="https://openai.test/v1", timeout=5, transport=httpx.MockTransport(self._openai),
                ),
                "mock": MockLlmChatAdapter(),
            },
            blob_store=self.store,
        )

    @staticmethod
    def _completion(content: str) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-intake",
            "model": "gpt-5-mini-2025-08-07",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9000, "completion_tokens": 900},
        })

    def test_byok_reserves_at_admission_and_reads_images(self):
        upload = self._upload(self.ctx, _png((40, 80, 120)))
        answer = extraction._mock_product_intake(SimpleNamespace(
            images=(object(),), schema_name=extraction.SCHEMA_NAME, json_schema={},
        ))
        self._use_fake_openai(self._completion(json.dumps(answer, ensure_ascii=False)))
        accepted = self._create({"upload_intent_ids": [upload], "text": "家用充电桩"})
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["execution_mode"], "byok")
        quote = intake["evidence"]["admission"]["quote"]
        self.assertEqual(quote["provider"], "openai")
        self.assertGreater(float(quote["max_amount"]), 0)
        with tenant_session(self.ctx) as session:
            reservation = session.get(
                SpendReservationRecord, intake["evidence"]["admission"]["reservation_id"],
            )
            self.assertEqual(reservation.state, "reserved")

        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "ready", intake.get("failure"))
        sent = json.loads(self.openai_requests[0].content)
        self.assertEqual(sent["response_format"]["json_schema"]["name"], extraction.SCHEMA_NAME)
        parts = sent["messages"][-1]["content"]
        self.assertEqual(sum(1 for part in parts if part["type"] == "image_url"), 1)
        self.assertEqual(self.openai_requests[0].headers["authorization"], "Bearer sk-ws-own")
        with tenant_session(self.ctx) as session:
            reservation = session.get(
                SpendReservationRecord, intake["evidence"]["admission"]["reservation_id"],
            )
            self.assertNotEqual(reservation.state, "reserved", "settled after the call")
        self.assertEqual(intake["evidence"]["extract"]["provider"], "openai")

    def test_a_failed_fetch_releases_the_unused_llm_reservation(self):
        self._use_fake_openai()
        self.web.routes[("shop.example", "/p/blocked")] = httpx.Response(403)
        accepted = self._create({"url": "https://shop.example/p/blocked"})
        reservation_id = self._get(accepted["intake_id"])["evidence"]["admission"]["reservation_id"]
        self._drain()
        self.assertEqual(self._get(accepted["intake_id"])["status"], "fetch_failed")
        self.assertEqual(self.openai_requests, [])
        with tenant_session(self.ctx) as session:
            reservation = session.get(SpendReservationRecord, reservation_id)
            self.assertEqual(reservation.state, "released")
            self.assertEqual(float(reservation.outstanding_budget_amount), 0.0)
        self.assertEqual(len(self._audits("product_intake.reservation_released")), 1)

    def test_byok_invalid_json_twice_is_extraction_failed_with_raw_output(self):
        self._use_fake_openai(self._completion("Sorry, here is the card: {name"),
                              self._completion("still not json"))
        accepted = self._create({"text": "家用充电桩"})
        self._drain()
        intake = self._get(accepted["intake_id"])
        self.assertEqual(intake["status"], "extraction_failed")
        self.assertEqual(intake["failure"]["code"], "invalid_json")
        self.assertEqual(len(self.openai_requests), 2, "re-asked once, then failed")
        record = self._record(accepted["intake_id"])
        self.assertEqual(
            record.extraction_json["raw_output"],
            ["Sorry, here is the card: {name", "still not json"],
        )


def test_storable_idempotency_key_keeps_short_keys_and_digests_long_ones():
    from app.library.service import IDEMPOTENCY_KEY_MAX_LENGTH, storable_idempotency_key

    short = "product-intake:pin_x:entry"
    assert storable_idempotency_key(short) == short
    assert storable_idempotency_key(None) is None
    exact = "k" * IDEMPOTENCY_KEY_MAX_LENGTH
    assert storable_idempotency_key(exact) == exact
    long_key = "product-intake:pin_" + "a" * 32 + ":promote:refobj_" + "b" * 32 + ":lib_" + "c" * 32
    assert len(long_key) > IDEMPOTENCY_KEY_MAX_LENGTH
    stored = storable_idempotency_key(long_key)
    assert len(stored) <= IDEMPOTENCY_KEY_MAX_LENGTH
    assert stored == storable_idempotency_key(long_key)
    assert stored != storable_idempotency_key(long_key + "x")
