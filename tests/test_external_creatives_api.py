"""Issue #168 -- external creatives join tests and are attributed by dimension value.

Real HTTP routes, the real Worker (reference gate, asset storage, governed
``llm_chat`` tagging under the controlled Mock and against a fake OpenAI),
real test plans and Mock generation Runs, the real experiment freeze and the
real learnings snapshot -- only the bucket and OpenAI are fakes.

* Upload: purpose ``external_creative``, rights declaration, the gate
  (rejection, EXIF stripped from the stored master), an AssetVersion of
  origin ``upload`` with no Run and no execution mode, a thumbnail and an
  MK code; replay by key and by content.
* Tagging: the product's vocabulary (test-plan factors), a schema-valid
  Mock suggestion; BYOK reserves at admission and settles after the call;
  an unusable answer leaves the asset taggable by hand.
* Confirm: the same ``dck_`` concept keys and signature a generated image
  with those values carries; append-only revisions; Approved review.
* Experiments: freeze accepts the external creative next to BYOK images;
  unconfirmed stays ``concept_identity_missing``; Mock + external is
  ``execution_mode_mixed``.
* Learnings: unconfirmed is ``external_untagged``; confirming re-places
  results already imported; the template row becomes attributable.
* Seeds: an optimize plan reads the confirmed labels.
* Negative tenant and permission paths.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from PIL import Image
from sqlalchemy import select, update

from tests.support.library_fixtures import jpeg_with_exif, png_bytes
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs
from tests.test_blob_store_railway_bucket import _FakeRailwayClient
from tests.test_library_reference_commands import _provision_finalized_intent
from tests.test_test_plans_api import PlanFixtures, openai_completion

from app.adapters.blob_store_railway_bucket import RailwayBucketBlobStore
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AdTestPlanRecord,
    AssetRecord,
    AssetVersionRecord,
    AuditEventRecord,
    ExternalCreativeRecord,
    ExternalCreativeTagConfirmationRecord,
    ExperimentVersionCandidateRecord,
    ExperimentVersionConceptRecord,
    LibraryEntryRecord,
    LibraryVersionRecord,
    ProductRecord,
    QualityReviewRecord,
    utcnow,
)
from app.entitlements.cost_models import SpendReservationRecord
from app.experiments.service import (
    ExperimentConflict,
    add_entry_v2,
    analyze_duplicates,
    create_experiment_v2,
    freeze_experiment_v2,
)
from app.external_creatives import tagging
from app.external_creatives import vocabulary as vocab
from app.external_creatives import v1_router as external_router
from app.external_creatives.worker import aspect_ratio
from app.generation import v1_router as generation_router
from app.generation.models import AssetQCAttemptRecord, RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.review import create_review, find_review_head
from app.library.contracts import ROLE_OWNER
from app.marketing import expression_catalog
from app.platform.canonical import _canonical_json, _sha256
from app.platform.tenancy import TenantContext
from app.worker_runtime import WorkerRuntime

TODAY = utcnow().date()
START = (TODAY - timedelta(days=14)).isoformat()
END = (TODAY - timedelta(days=8)).isoformat()
RIGHTS = {"made_by": "designer", "permission_confirmed": True, "note": "Lovart 出图"}


def _png(colour: tuple[int, int, int], size=(400, 500)) -> bytes:
    return png_bytes(size, colour)


class ExternalFixtures(PlanFixtures):
    def setUp(self) -> None:
        super().setUp()
        self.bucket = _FakeRailwayClient()
        self.store = RailwayBucketBlobStore(
            bucket="marketai-test", endpoint_url="https://storage.invalid", client=self.bucket,
        )
        for module in (external_router, generation_router):
            patcher = patch.object(module, "_blob_store", self.store)
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- helpers ----------------------------------------------------------

    def _use_fake_openai(self, *answers) -> None:
        """A fake OpenAI that also reads blob image refs from the fake bucket."""
        import httpx

        from app.providers.dispatch import ExternalCallCoordinator
        from app.providers.llm_chat import LlmChatClient
        from app.providers.llm_mock import MockLlmChatAdapter
        from app.providers.openai_chat import OpenAIChatAdapter
        from tests.test_test_plans_api import PROTECTOR

        self.openai_answers = list(answers)

        def handler(request: httpx.Request) -> httpx.Response:
            self.openai_requests.append(request)
            return self.openai_answers.pop(0)

        self.llm_client = LlmChatClient(
            coordinator=ExternalCallCoordinator(tenant_session),
            session_factory=tenant_session,
            protector=PROTECTOR,
            adapters={
                "openai": OpenAIChatAdapter(
                    base_url="https://openai.test/v1", timeout=5, transport=httpx.MockTransport(handler),
                ),
                "mock": MockLlmChatAdapter(),
            },
            blob_store=self.store,
        )

    def _drain(self) -> None:
        quarantine_foreign_jobs(self.ctx.workspace_id)
        runtime = WorkerRuntime(owner=f"external-{uuid.uuid4().hex[:6]}")
        runtime.blob_store = self.store
        if self.llm_client is not None:
            runtime._llm_chat_client = self.llm_client
        for _ in range(200):
            if not asyncio.run(runtime.run_once()):
                break

    def _upload(self, data: bytes, *, ctx: TenantContext | None = None, purpose: str = "external_creative",
                content_type: str = "image/png", filename: str = "ugc.png") -> str:
        ctx = ctx or self.ctx
        extension = "jpg" if content_type == "image/jpeg" else "png"
        key = f"committed/{ctx.workspace_id}/{purpose}/2026/10/12/{uuid.uuid4().hex}.{extension}"
        self.bucket.put_test_object(key, data, content_type=content_type)
        intent_id, _ = _provision_finalized_intent(
            ctx, committed_key=key, committed_version=f"sha256:{hashlib.sha256(data).hexdigest()}",
            purpose=purpose, raw=data, write_file=False, content_type=content_type, filename=filename,
        )
        return intent_id

    def _submit(self, body: dict, *, key: str | None = None, expect: int = 202) -> dict:
        response = self.client.post("/api/v1/external-creatives", json=body, headers=self._headers(key))
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _creative(self, creative_id: str, *, expect: int = 200) -> dict:
        response = self.client.get(f"/api/v1/external-creatives/{creative_id}")
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _confirm(self, creative_id: str, body: dict | None = None, *, key: str | None = None,
                 expect: int = 200) -> dict:
        response = self.client.post(
            f"/api/v1/external-creatives/{creative_id}/confirm", json=body or {}, headers=self._headers(key),
        )
        self.assertEqual(response.status_code, expect, response.text)
        return response.json()

    def _plan_create(self, body: dict, *, expect: int = 202) -> dict:
        return PlanFixtures._create(self, body, expect=expect)

    def _uploaded(self, product_id: str, *images: bytes, rights: dict | None = None) -> list[dict]:
        uploads = [self._upload(data) for data in images]
        accepted = self._submit({"product_id": product_id, "upload_intent_ids": uploads, "rights": rights or RIGHTS})
        self._drain()
        return [self._creative(item["external_creative_id"]) for item in accepted["items"]]

    def _ready_product(self) -> tuple[str, dict]:
        product_id = self._product()
        plan = self._ready_plan(product_id=product_id)
        return product_id, plan

    def _recipe(self, plan_id: str) -> dict:
        with tenant_session(self.ctx) as session:
            return dict(session.get(AdTestPlanRecord, plan_id).recipe_json)

    def _sample_items(self, plan_id: str) -> list[RunItemRecord]:
        run = self._run(plan_id, "sample")
        self._drain()
        return self._items(run["run_id"])

    def _version_of(self, run_item_id: str) -> str:
        with tenant_session(self.ctx) as session:
            return session.scalars(
                select(RunItemAssetVersionBindingRecord.asset_version_id).where(
                    RunItemAssetVersionBindingRecord.workspace_id == self.ctx.workspace_id,
                    RunItemAssetVersionBindingRecord.run_item_id == run_item_id,
                )
            ).first()

    def _tags_for(self, creative: dict, cells: dict[str, str], plan_id: str, recipe: dict) -> list[dict]:
        """The vocabulary choices matching a plan combination's cells (by label)."""
        labels = {
            factor["key"]: {value["id"]: value["label"] for value in factor["values"]}
            for factor in recipe["factors"]
        }
        tags = []
        for factor in creative["vocabulary"]["factors"]:
            value_id = cells.get(factor["factor_id"])
            if value_id is None:
                continue
            label = labels[factor["factor_id"]][value_id]
            match = next(value for value in factor["values"] if value["label"] == label)
            tags.append({"factor_id": factor["factor_id"], "value_id": match["value_id"]})
        return tags

    def _audits(self, event_type: str) -> list[AuditEventRecord]:  # noqa: D401 -- same as base
        return PlanFixtures._audits(self, event_type)


class TestUploadAndTagging(ExternalFixtures):
    def test_an_upload_is_gated_stored_as_an_external_asset_and_tagged_under_mock(self):
        product_id, plan = self._ready_product()
        recipe = self._recipe(plan["plan_id"])
        uploads = [
            self._upload(_png((200, 40, 40))),
            # Longer than external_creatives.filename's varchar(300).
            self._upload(_png((40, 200, 40), size=(1080, 1920)), filename="长" * 400 + ".png"),
        ]
        accepted = self._submit({"product_id": product_id, "upload_intent_ids": uploads, "rights": RIGHTS})
        self.assertEqual(len(accepted["items"]), 2)
        first = accepted["items"][0]
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["execution_mode"], "mock")
        self.assertFalse(first["replayed"])
        self.assertEqual(first["rights"]["made_by"], "designer")
        self.assertEqual(first["rights"]["declared_by"], "member_a")
        # The plan's factors, then the expression mechanism catalog every product has (#329).
        self.assertEqual(
            [factor["factor_id"] for factor in first["vocabulary"]["factors"]],
            [factor["key"] for factor in recipe["factors"]] + [vocab.CATALOG_FACTOR_ID],
        )
        self.assertEqual(
            [factor["source"] for factor in first["vocabulary"]["factors"]],
            [vocab.SOURCE_TEST_PLAN] * len(recipe["factors"]) + [vocab.SOURCE_CATALOG],
        )
        self.assertEqual(
            {d["direction_key"] for d in first["vocabulary"]["directions"]},
            {d["direction_id"] for d in plan["directions"]},
        )

        self._drain()
        creatives = [self._creative(item["external_creative_id"]) for item in accepted["items"]]
        for creative in creatives:
            self.assertEqual(creative["status"], "ready", creative.get("failure"))
            self.assertRegex(creative["asset_code"], r"^MK-[0-9A-HJKMNP-TV-Z]{6}$")
            self.assertIsNotNone(creative["thumbnail"])
            suggestion = creative["suggestion"]
            self.assertEqual(suggestion["execution_mode"], "mock")
            self.assertEqual(len(suggestion["factors"]), len(creative["vocabulary"]["factors"]))
            for item in suggestion["factors"]:
                factor = next(f for f in creative["vocabulary"]["factors"] if f["factor_id"] == item["factor_id"])
                self.assertIn(item["value_id"], {value["value_id"] for value in factor["values"]})
                self.assertTrue(0.55 <= item["confidence"] <= 0.9)
                self.assertIn("受控 Mock", item["reason"])
        self.assertEqual(creatives[1]["aspect_ratio"], "9:16")

        with tenant_session(self.ctx) as session:
            version = session.get(AssetVersionRecord, creatives[0]["asset_version_id"])
            asset = session.get(AssetRecord, version.asset_id)
            self.assertEqual(version.origin_kind, "upload")
            self.assertIsNone(version.execution_mode)
            self.assertIsNone(version.producing_run_item_id)
            self.assertFalse(version.truth_dependency_expected)
            self.assertEqual(asset.source, "external")
            self.assertIsNone(asset.run_id)
            self.assertEqual(asset.product_id, product_id)
            self.assertIsNotNone(version.thumbnail_object_key)
            self.assertEqual(
                session.scalars(select(SpendReservationRecord).where(
                    SpendReservationRecord.workspace_id == self.ctx.workspace_id,
                )).all(),
                [],
                "Mock tagging reserves nothing real",
            )

        gallery = self.client.get(
            "/api/v1/generation/assets", params={"product_id": product_id, "origin_kind": "upload"},
        )
        self.assertEqual(gallery.status_code, 200, gallery.text)
        items = gallery.json()["items"]
        self.assertEqual({item["id"] for item in items}, {c["asset_version_id"] for c in creatives})
        self.assertTrue(all(item["run_id"] == "" for item in items))
        self.assertIn({"value": "upload", "count": 2}, gallery.json()["facets"]["origins"])
        detail = self.client.get(f"/api/v1/generation/asset-versions/{creatives[0]['asset_version_id']}").json()
        regenerate = next(action for action in detail["allowed_actions"] if action["action"] == "regenerate")
        self.assertFalse(regenerate["allowed"])

        listed = self.client.get("/api/v1/external-creatives", params={"product_id": product_id})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["items"]), 2)
        self.assertEqual(len(self._audits("external_creative.created")), 2)
        steps = {(event.details_json or {}).get("step") for event in self._audits("external_creative.step")}
        self.assertEqual(steps, {"gate", "asset", "tag"})
        self.assert_stored_widths()

    def test_replays_by_key_and_by_content(self):
        product_id, _plan = self._ready_product()
        data = _png((10, 20, 30))
        upload = self._upload(data)
        first = self._submit({"product_id": product_id, "upload_intent_ids": [upload], "rights": RIGHTS}, key="k-1")
        again = self._submit({"product_id": product_id, "upload_intent_ids": [upload], "rights": RIGHTS}, key="k-1")
        self.assertTrue(again["items"][0]["replayed"])
        self.assertEqual(again["items"][0]["external_creative_id"], first["items"][0]["external_creative_id"])
        conflict = self._submit(
            {"product_id": product_id, "upload_intent_ids": [upload], "rights": {**RIGHTS, "made_by": "self"}},
            key="k-1", expect=409,
        )
        self.assertEqual(conflict["code"], "idempotency_key_reused")
        same_bytes = self._submit(
            {"product_id": product_id, "upload_intent_ids": [self._upload(data)], "rights": RIGHTS},
        )
        self.assertTrue(same_bytes["items"][0]["replayed"])
        self.assertEqual(same_bytes["items"][0]["external_creative_id"], first["items"][0]["external_creative_id"])
        with tenant_session(self.ctx) as session:
            self.assertEqual(len(session.scalars(select(ExternalCreativeRecord).where(
                ExternalCreativeRecord.workspace_id == self.ctx.workspace_id,
            )).all()), 1)

    def test_exif_metadata_never_reaches_the_stored_master(self):
        product_id, _plan = self._ready_product()
        source = jpeg_with_exif((640, 640))
        self.assertTrue(Image.open(io.BytesIO(source)).getexif())
        upload = self._upload(source, content_type="image/jpeg", filename="camera.jpg")
        accepted = self._submit({"product_id": product_id, "upload_intent_ids": [upload], "rights": RIGHTS})
        self._drain()
        creative = self._creative(accepted["items"][0]["external_creative_id"])
        self.assertEqual(creative["status"], "ready", creative.get("failure"))
        self.assertEqual(creative["mime_type"], "image/jpeg")
        with tenant_session(self.ctx) as session:
            version = session.get(AssetVersionRecord, creative["asset_version_id"])
            record = session.get(ExternalCreativeRecord, creative["external_creative_id"])
            self.assertTrue(record.evidence_json["asset"]["metadata_stripped"])
            stored = self.bucket.objects[version.committed_object_key]
        self.assertNotEqual(hashlib.sha256(stored).hexdigest(), hashlib.sha256(source).hexdigest())
        self.assertFalse(Image.open(io.BytesIO(stored)).getexif())

    def test_the_gate_rejects_an_image_whose_bytes_lie_about_their_type(self):
        product_id, _plan = self._ready_product()
        jpeg = io.BytesIO()
        Image.new("RGB", (300, 300), (1, 2, 3)).save(jpeg, format="JPEG")
        upload = self._upload(jpeg.getvalue(), content_type="image/png")
        accepted = self._submit({"product_id": product_id, "upload_intent_ids": [upload], "rights": RIGHTS})
        self._drain()
        creative = self._creative(accepted["items"][0]["external_creative_id"])
        self.assertEqual(creative["status"], "rejected")
        self.assertEqual(creative["failure"]["code"], "reference_gate_rejected")
        self.assertIsNone(creative["asset_version_id"])
        refused = self._confirm(creative["external_creative_id"], {"tags": []}, expect=409)
        self.assertEqual(refused["code"], "external_creative_not_ready")

    def test_admission_refusals(self):
        product_id, _plan = self._ready_product()
        upload = self._upload(_png((1, 1, 1)))
        for body, status, code in (
            ({"product_id": product_id, "upload_intent_ids": [upload], "rights": {"made_by": "self"}},
             422, "rights_confirmation_required"),
            ({"product_id": "prod_missing", "upload_intent_ids": [upload], "rights": RIGHTS}, 404, "product_not_found"),
            ({"product_id": product_id, "upload_intent_ids": ["ui_missing"], "rights": RIGHTS},
             404, "upload_intent_not_found"),
            ({"product_id": product_id, "upload_intent_ids": [self._upload(_png((2, 2, 2)), purpose="product_intake_image")],
              "rights": RIGHTS}, 422, "upload_purpose_not_allowed"),
        ):
            refused = self._submit(body, expect=status)
            self.assertEqual(refused["code"], code, refused)
        too_many = self.client.post(
            "/api/v1/external-creatives", headers=self._headers(),
            json={"product_id": product_id, "upload_intent_ids": [f"ui_{i}" for i in range(11)], "rights": RIGHTS},
        )
        self.assertEqual(too_many.status_code, 422)
        no_key = self.client.post(
            "/api/v1/external-creatives",
            json={"product_id": product_id, "upload_intent_ids": [upload], "rights": RIGHTS},
        )
        self.assertIn(no_key.status_code, (400, 422))

    def test_a_product_without_test_plans_is_tagged_by_the_expression_catalog_and_still_usable_as_a_seed(self):
        # Issue #329: the expression mechanism catalog is every product's vocabulary, so a product
        # without test plans is tagged too (before #329 it was stored untagged).
        product_id = self._product()
        [creative] = self._uploaded(product_id, _png((90, 90, 90)))
        self.assertEqual(creative["status"], "ready")
        [factor] = creative["vocabulary"]["factors"]
        self.assertEqual((factor["factor_id"], factor["source"]), (vocab.CATALOG_FACTOR_ID, vocab.SOURCE_CATALOG))
        self.assertEqual(len(factor["values"]), len(expression_catalog.ENTRIES))
        [suggested] = creative["suggestion"]["factors"]
        self.assertEqual(suggested["factor_id"], vocab.CATALOG_FACTOR_ID)
        self.assertIn(suggested["value_id"], {value["value_id"] for value in factor["values"]})
        self.assertIn("受控 Mock", suggested["reason"])
        confirmed = self._confirm(creative["external_creative_id"], {
            "tags": [{"factor_id": vocab.CATALOG_FACTOR_ID, "value_id": suggested["value_id"]}],
        })
        self.assertEqual(confirmed["status"], "confirmed")
        [tag] = confirmed["confirmation"]["tags"]
        self.assertEqual((tag["factor_id"], tag["value_id"], tag["source"]),
                         (vocab.CATALOG_FACTOR_ID, suggested["value_id"], "ai"))
        # A catalog value is no test-plan concept: the creative is tagged, not experiment-ready.
        self.assertFalse(confirmed["confirmation"]["experiment_ready"])
        self.assertIsNotNone(confirmed["confirmation"]["review_id"])
        approved = self.client.get(
            "/api/v1/generation/assets", params={"product_id": product_id, "review_value": "approved"},
        ).json()["items"]
        self.assertEqual([item["id"] for item in approved], [creative["asset_version_id"]])
        self.assertEqual(approved[0]["origin_kind"], "upload")


class TestByokTagging(ExternalFixtures):
    def _answer(self, creative: dict, *, pick: int = 0) -> str:
        factors = {}
        for index, factor in enumerate(creative["vocabulary"]["factors"]):
            factors[tagging.factor_key(index)] = {
                "value_id": factor["values"][pick % len(factor["values"])]["value_id"] if index else "none",
                "confidence": 0.81,
                "reason": "画面里能看到车主插枪",
            }
        return json.dumps({"factors": factors, "summary": "车库里的充电场景"}, ensure_ascii=False)

    def test_byok_tagging_reserves_at_admission_and_settles_after_the_call(self):
        self._bind_openai()
        with self._mock_drafting():
            product_id, _plan = self._ready_product()
        upload = self._upload(_png((120, 60, 30)))
        accepted = self._submit({"product_id": product_id, "upload_intent_ids": [upload], "rights": RIGHTS})
        creative = accepted["items"][0]
        self.assertEqual(creative["execution_mode"], "byok")
        with tenant_session(self.ctx) as session:
            record = session.get(ExternalCreativeRecord, creative["external_creative_id"])
            admission = record.evidence_json["admission"]
            self.assertEqual(admission["quote"]["provider"], "openai")
            self.assertGreater(float(admission["quote"]["max_amount"]), 0)
            reservation = session.get(SpendReservationRecord, admission["reservation_id"])
            self.assertEqual(reservation.state, "reserved")
            reservation_id = admission["reservation_id"]
        self._use_fake_openai(openai_completion(self._answer(creative)))
        self._drain()
        tagged = self._creative(creative["external_creative_id"])
        self.assertEqual(tagged["status"], "ready", tagged.get("failure"))
        self.assertEqual(tagged["suggestion"]["provider"], "openai")
        self.assertIsNone(tagged["suggestion"]["factors"][0]["value_id"], "none of these -> null")
        self.assertEqual(tagged["suggestion"]["factors"][1]["confidence"], 0.81)
        sent = json.loads(self.openai_requests[0].content)
        self.assertEqual(sent["response_format"]["json_schema"]["name"], tagging.SCHEMA_NAME)
        parts = sent["messages"][-1]["content"]
        self.assertEqual(sum(1 for part in parts if part["type"] == "image_url"), 1)
        with tenant_session(self.ctx) as session:
            reservation = session.get(SpendReservationRecord, reservation_id)
            self.assertNotEqual(reservation.state, "reserved", "settled after the call")

    def test_an_unusable_answer_leaves_the_asset_taggable_by_hand(self):
        self._bind_openai()
        with self._mock_drafting():
            product_id, plan = self._ready_product()
        upload = self._upload(_png((7, 70, 140)))
        accepted = self._submit({"product_id": product_id, "upload_intent_ids": [upload], "rights": RIGHTS})
        self._use_fake_openai(openai_completion("not json"), openai_completion("still not json"))
        self._drain()
        creative = self._creative(accepted["items"][0]["external_creative_id"])
        self.assertEqual(creative["status"], "tagging_failed")
        self.assertEqual(creative["failure"]["code"], "invalid_json")
        self.assertEqual(creative["failure"]["suggested_action"], "tag_manually")
        self.assertIsNotNone(creative["asset_version_id"])
        factor = creative["vocabulary"]["factors"][0]
        confirmed = self._confirm(creative["external_creative_id"], {
            "tags": [{"factor_id": factor["factor_id"], "value_id": factor["values"][0]["value_id"]}],
        })
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["confirmation"]["tags"][0]["source"], "user")


class TestConfirmIdentity(ExternalFixtures):
    def test_confirm_writes_the_concept_identity_a_generated_image_carries(self):
        product_id, plan = self._ready_product()
        recipe = self._recipe(plan["plan_id"])
        items = self._sample_items(plan["plan_id"])
        generated = items[0]
        identity = generated.combination_identity_json
        [creative] = self._uploaded(product_id, _png((33, 66, 99)))

        with tenant_session(self.ctx) as session:
            template_entry_id = session.get(AdTestPlanRecord, plan["plan_id"]).template_entry_id
        cells = {
            concept["value"]["dimension_id"]: concept["value"]["value_id"]
            for concept in identity["concepts"] if concept["value"]["dimension_id"] != "direction"
        }
        direction_key = next(
            concept["value"]["value_id"] for concept in identity["concepts"]
            if concept["value"]["dimension_id"] == "direction"
        )
        body = {
            "tags": self._tags_for(creative, cells, plan["plan_id"], recipe),
            "direction": {"plan_id": plan["plan_id"], "direction_key": direction_key},
        }
        confirmed = self._confirm(creative["external_creative_id"], body)
        self.assertEqual(confirmed["status"], "confirmed")
        confirmation = confirmed["confirmation"]
        self.assertEqual(confirmation["revision"], 1)
        self.assertTrue(confirmation["experiment_ready"])
        self.assertTrue(confirmation["attributable"])
        self.assertEqual(confirmation["round"], 1)
        self.assertEqual(confirmation["direction"]["direction_key"], direction_key)
        self.assertEqual(confirmation["stable_dimension_signature"], generated.stable_dimension_signature)

        with tenant_session(self.ctx) as session:
            row = session.scalars(select(ExternalCreativeTagConfirmationRecord).where(
                ExternalCreativeTagConfirmationRecord.external_creative_id == creative["external_creative_id"],
            )).one()
            self.assertEqual(
                {(c["dimension_key"], c["concept_key_id"]) for c in row.concepts_json},
                {(c["dimension_key"], c["concept_key_id"]) for c in identity["concepts"]},
            )
            self.assertTrue(all(c["dimension_key"].startswith(f"{template_entry_id}:") for c in row.concepts_json))
            self.assertEqual(row.plan_id, plan["plan_id"])
            self.assertEqual(row.cells_json, cells)
            head = find_review_head(session, self.ctx, creative["asset_version_id"])
            self.assertEqual(head.review_value, "approved")
            self.assertEqual(row.review_id, head.id)
        self.assertEqual(len(self._audits("external_creative.confirmed")), 1)

        replay = self._confirm(creative["external_creative_id"], body)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["confirmation"]["revision"], 1)
        changed = self._confirm(creative["external_creative_id"], {"tags": body["tags"][:1]})
        self.assertEqual(changed["confirmation"]["revision"], 2)
        self.assertIsNone(changed["confirmation"]["direction"])
        with tenant_session(self.ctx) as session:
            rows = session.scalars(select(ExternalCreativeTagConfirmationRecord).where(
                ExternalCreativeTagConfirmationRecord.external_creative_id == creative["external_creative_id"],
            ).order_by(ExternalCreativeTagConfirmationRecord.revision)).all()
            self.assertEqual([r.revision for r in rows], [1, 2])
            self.assertEqual(rows[1].supersedes_id, rows[0].id)
            self.assertEqual(len(rows[0].concepts_json), len(identity["concepts"]), "history is not rewritten")
        self.assert_stored_widths()

    def test_a_plan_that_never_generated_gets_its_template_entry_not_a_version(self):
        product_id, plan = self._ready_product()
        [creative] = self._uploaded(product_id, _png((5, 5, 200)))
        factor = creative["vocabulary"]["factors"][0]
        confirmed = self._confirm(creative["external_creative_id"], {
            "tags": [{"factor_id": factor["factor_id"], "value_id": factor["values"][0]["value_id"]}],
        })
        self.assertTrue(confirmed["confirmation"]["experiment_ready"])
        with tenant_session(self.ctx) as session:
            entry_id = session.get(AdTestPlanRecord, plan["plan_id"]).template_entry_id
            self.assertTrue(entry_id)
            entry = session.get(LibraryEntryRecord, entry_id)
            self.assertIsNone(entry.current_version_id, "no AI template version is published without generate")
        # The plan's later Run reuses that entry, so its concept keys match.
        items = self._sample_items(plan["plan_id"])
        with tenant_session(self.ctx) as session:
            row = session.scalars(select(ExternalCreativeTagConfirmationRecord).where(ExternalCreativeTagConfirmationRecord.workspace_id == self.ctx.workspace_id)).first()
            generated_keys = {
                concept["concept_key_id"] for item in items for concept in item.combination_identity_json["concepts"]
            }
        [external] = row.concepts_json
        self.assertEqual(external["dimension_key"], f"{entry_id}:{factor['factor_id']}")
        self.assertTrue(all(
            concept["dimension_key"].startswith(f"{entry_id}:")
            for item in items for concept in item.combination_identity_json["concepts"]
        ), "the Run reused the entry the confirmation created")
        same_value = [
            concept for item in items for concept in item.combination_identity_json["concepts"]
            if concept["dimension_key"] == external["dimension_key"]
            and concept["value"]["value_id"] == external["value"]["value_id"]
        ]
        self.assertTrue(all(concept["concept_key_id"] == external["concept_key_id"] for concept in same_value))
        self.assertTrue(same_value or external["concept_key_id"] not in generated_keys)

    def test_unknown_factor_value_or_direction_is_refused(self):
        product_id, plan = self._ready_product()
        [creative] = self._uploaded(product_id, _png((250, 250, 0)))
        factor = creative["vocabulary"]["factors"][0]
        for body, code in (
            ({"tags": [{"factor_id": "nope", "value_id": "v1"}]}, "unknown_factor"),
            ({"tags": [{"factor_id": factor["factor_id"], "value_id": "v99"}]}, "unknown_value"),
            ({"tags": [{"factor_id": factor["factor_id"], "value_id": "v1"},
                       {"factor_id": factor["factor_id"], "value_id": "v1"}]}, "duplicate_factor"),
            ({"tags": [], "direction": {"plan_id": plan["plan_id"], "direction_key": "dir-99"}}, "unknown_direction"),
        ):
            refused = self._confirm(creative["external_creative_id"], body, expect=422)
            self.assertEqual(refused["code"], code)


class TestExperimentsLearningsAndSeeds(ExternalFixtures):
    def _offer(self, product_version_id: str) -> str:
        entry_id = f"le_offer_{uuid.uuid4().hex[:8]}"
        version_id = f"ov_{uuid.uuid4().hex[:8]}"
        content = {
            "product_version_id": product_version_id,
            "market_scope": ["US"],
            "landing_page_url": "https://example.test/offer",
            "valid_from": "2026-01-01",
            "valid_to": "2027-12-31",
            "source_market_snapshot": {
                "market": "US", "placement": "feed", "locale": "en-US", "country": "US", "currency": "USD",
            },
        }
        with tenant_session(self.owner_ctx(self.ctx)) as session:
            session.add(LibraryEntryRecord(
                id=entry_id, workspace_id=self.ctx.workspace_id, entry_type="offer", display_name="Offer",
            ))
            session.flush()
            session.add(LibraryVersionRecord(
                id=version_id, workspace_id=self.ctx.workspace_id, entry_id=entry_id, entry_type="offer",
                version_no=1, content_json=content,
                content_hash=_sha256(_canonical_json(content).encode("utf-8")),
                state="effective", eligibility="eligible", published_by="system",
            ))
        return version_id

    def _experiment(self, product_id: str, asset_version_ids: list[str]) -> tuple[str, int]:
        with tenant_session(self.ctx) as session:
            product = session.get(ProductRecord, product_id)
            product_version_id = session.scalars(select(LibraryVersionRecord.id).where(
                LibraryVersionRecord.workspace_id == self.ctx.workspace_id,
                LibraryVersionRecord.entry_id == product.product_profile_entry_id,
                LibraryVersionRecord.state == "effective",
            )).first()
        offer_version_id = self._offer(product_version_id)
        experiment = create_experiment_v2(self.ctx, product_id=product_id, name="外部素材实验", draft_context={
            "market": "US", "currency": "USD", "placement": "feed",
            "product_version_id": product_version_id, "offer_version_id": offer_version_id,
            "budget": {"daily_amount": "50", "total_amount": "700"},
            "schedule": {"start_at": "2026-11-01T00:00:00+00:00", "end_at": "2026-11-15T00:00:00+00:00",
                         "timezone": "UTC"},
            "attribution": {"click_window_days": 7, "view_window_days": 1},
            "statistical_plan": {"primary_metric": "ctr", "ctr_threshold": "0.01", "alpha": "0.05",
                                 "bootstrap_samples": 2000},
        })
        revision = 1
        for version_id in asset_version_ids:
            revision = add_entry_v2(
                self.ctx, experiment["id"], asset_version_id=version_id, expected_revision=revision,
            )["revision"]
        return experiment["id"], revision

    def _approve_generated(self, version_id: str, run_item_id: str) -> None:
        with tenant_session(self.ctx) as session:
            session.execute(
                update(AssetVersionRecord)
                .where(AssetVersionRecord.workspace_id == self.ctx.workspace_id, AssetVersionRecord.id == version_id)
                .values(execution_mode="byok")
            )
            if find_review_head(session, self.ctx, version_id) is None:
                create_review(session, self.ctx, version_id, "approved", "ok", actor_id="member_a")
            has_qc = session.scalars(select(AssetQCAttemptRecord.id).where(
                AssetQCAttemptRecord.workspace_id == self.ctx.workspace_id,
                AssetQCAttemptRecord.asset_version_id == version_id,
            )).first()
            if has_qc is None:
                session.add(AssetQCAttemptRecord(
                    id=f"qc_{uuid.uuid4().hex[:12]}", workspace_id=self.ctx.workspace_id,
                    asset_version_id=version_id, run_item_id=run_item_id, qc_policy_version="qc-v1",
                    rule_set_version="rules-v1", input_hash="0" * 64, execution_mode="byok",
                    state="passed", attempt_sequence=1,
                ))

    def _different_tags(self, creative: dict, generated: RunItemRecord, recipe: dict) -> list[dict]:
        """One value per factor that differs from the generated image's (no duplicate concepts)."""
        cells = {
            c["value"]["dimension_id"]: c["value"]["value_id"]
            for c in generated.combination_identity_json["concepts"] if c["value"]["dimension_id"] != "direction"
        }
        labels = {f["key"]: {v["id"]: v["label"] for v in f["values"]} for f in recipe["factors"]}
        tags = []
        for factor in creative["vocabulary"]["factors"]:  # the catalog factor (#329) is no plan factor
            avoid = labels.get(factor["factor_id"], {}).get(cells.get(factor["factor_id"]))
            other = next((v for v in factor["values"] if v["label"] != avoid), None)
            if other is not None:
                tags.append({"factor_id": factor["factor_id"], "value_id": other["value_id"]})
        return tags

    def test_a_confirmed_external_creative_freezes_into_an_experiment_with_byok_images(self):
        product_id, plan = self._ready_product()
        recipe = self._recipe(plan["plan_id"])
        generated = self._sample_items(plan["plan_id"])[0]
        generated_version = self._version_of(generated.id)
        self._approve_generated(generated_version, generated.id)
        [creative] = self._uploaded(product_id, _png((210, 30, 120)))
        external_version = creative["asset_version_id"]

        experiment_id, revision = self._experiment(product_id, [generated_version, external_version])
        with self.assertRaisesRegex(ExperimentConflict, f"concept_identity_missing:{external_version}"):
            analyze_duplicates(self.ctx, experiment_id)

        self._confirm(creative["external_creative_id"], {"tags": self._different_tags(creative, generated, recipe)})
        analysis = analyze_duplicates(self.ctx, experiment_id)
        self.assertFalse(analysis["has_duplicates"], analysis["duplicate_groups"])
        frozen = freeze_experiment_v2(self.ctx, experiment_id, revision)
        self.assertEqual(frozen["state"], "frozen")
        with tenant_session(self.ctx) as session:
            candidate = session.scalars(select(ExperimentVersionCandidateRecord).where(
                ExperimentVersionCandidateRecord.workspace_id == self.ctx.workspace_id,
                ExperimentVersionCandidateRecord.asset_version_id == external_version,
            )).one()
            self.assertEqual(candidate.source_kind, "upload")
            self.assertEqual(candidate.execution_mode, "byok")
            self.assertIsNone(candidate.source_run_item_id)
            self.assertEqual(candidate.source_evidence_json["concept_source"]["source_kind"], "external_tag_confirmation")
            concepts = session.scalars(select(ExperimentVersionConceptRecord).where(
                ExperimentVersionConceptRecord.experiment_version_candidate_id == candidate.id,
            )).all()
            confirmation = session.scalars(select(ExternalCreativeTagConfirmationRecord).where(ExternalCreativeTagConfirmationRecord.workspace_id == self.ctx.workspace_id)).one()
            self.assertEqual(
                {(c.dimension_key, c.concept_key_id) for c in concepts},
                {(c["dimension_key"], c["concept_key_id"]) for c in confirmation.concepts_json},
            )
            generated_dimensions = {c["dimension_key"] for c in generated.combination_identity_json["concepts"]}
            self.assertTrue({c.dimension_key for c in concepts} <= generated_dimensions)

    def test_an_external_creative_never_mixes_with_mock_images(self):
        product_id, plan = self._ready_product()
        recipe = self._recipe(plan["plan_id"])
        generated = self._sample_items(plan["plan_id"])[0]
        generated_version = self._version_of(generated.id)
        with tenant_session(self.ctx) as session:
            create_review(session, self.ctx, generated_version, "approved", "ok", actor_id="member_a")
        [creative] = self._uploaded(product_id, _png((1, 120, 240)))
        self._confirm(creative["external_creative_id"], {"tags": self._different_tags(creative, generated, recipe)})
        experiment_id, revision = self._experiment(product_id, [generated_version, creative["asset_version_id"]])
        with self.assertRaisesRegex(ExperimentConflict, "execution_mode_mixed"):
            freeze_experiment_v2(self.ctx, experiment_id, revision)

    def test_learnings_place_results_once_the_tags_are_confirmed(self):
        product_id, plan = self._ready_product()
        recipe = self._recipe(plan["plan_id"])
        generated = self._sample_items(plan["plan_id"])
        [creative] = self._uploaded(product_id, _png((44, 55, 66)))
        code = creative["asset_code"]

        def template_row() -> dict:
            response = self.client.get(f"/api/v1/products/{product_id}/results/template")
            self.assertEqual(response.status_code, 200, response.text)
            return next(row for row in response.json()["rows"] if row["asset_code"] == code)

        self.assertFalse(template_row()["attributable"])
        preview = self.client.post("/api/v1/results/imports", headers=self._headers(), json={
            "product_id": product_id, "source": "manual", "currency": "USD",
            "rows": [{"asset_code": code, "date_start": START, "date_end": END, "impressions": 4000, "clicks": 80,
                      "spend": "40", "conversions": 4}],
        })
        self.assertEqual(preview.status_code, 201, preview.text)
        self.assertEqual(preview.json()["rows"][0]["status"], "unattributable")
        self.assertEqual(preview.json()["rows"][0]["evidence_mode"], "production")
        committed = self.client.post(
            f"/api/v1/results/imports/{preview.json()['import_id']}/commit", headers=self._headers(),
        )
        self.assertEqual(committed.status_code, 200, committed.text)
        before = self.client.get(f"/api/v1/products/{product_id}/learnings").json()
        self.assertEqual(before["unattributable"][0]["reason_code"], "external_untagged")

        cells = {
            c["value"]["dimension_id"]: c["value"]["value_id"]
            for c in generated[0].combination_identity_json["concepts"] if c["value"]["dimension_id"] != "direction"
        }
        self._confirm(creative["external_creative_id"], {"tags": self._tags_for(creative, cells, plan["plan_id"], recipe)})
        after = self.client.get(f"/api/v1/products/{product_id}/learnings").json()
        self.assertNotEqual(after["snapshot_id"], before["snapshot_id"], "confirming re-placed the imported results")
        self.assertEqual(after["unattributable"], [])
        self.assertEqual(after["status"], "ready")
        [round_] = after["rounds"]
        self.assertEqual(round_["plan_id"], plan["plan_id"])
        self.assertEqual(round_["totals"]["impressions"], 4000)
        counted = [
            value for factor in round_["factors"] for value in factor["values"]
            if value["value_id"] == cells.get(factor["key"])
        ]
        self.assertTrue(counted)
        self.assertTrue(all(value["n"]["assets"] == 1 for value in counted), counted)
        row = template_row()
        self.assertTrue(row["attributable"])
        self.assertEqual(row["round_number"], 1)
        self.assert_stored_widths()

    def test_an_optimize_plan_reads_the_confirmed_labels_of_an_external_seed(self):
        product_id, plan = self._ready_product()
        recipe = self._recipe(plan["plan_id"])
        [creative] = self._uploaded(product_id, _png((140, 140, 20)))
        factor = creative["vocabulary"]["factors"][0]
        value = factor["values"][0]
        self._confirm(creative["external_creative_id"], {
            "tags": [{"factor_id": factor["factor_id"], "value_id": value["value_id"]}],
        })
        accepted = self._plan_create({
            "product_id": product_id, "goal": "optimize", "seed_asset_version_ids": [creative["asset_version_id"]],
        })
        with tenant_session(self.ctx) as session:
            seeds = session.get(AdTestPlanRecord, accepted["plan_id"]).recipe_json["seeds"]
        self.assertEqual(seeds["winners"][0]["asset_version_id"], creative["asset_version_id"])
        self.assertEqual(seeds["winners"][0]["dimension_labels"], {factor["label"]: value["label"]})
        self.assertTrue(recipe["factors"])


class TestTenancyAndPermissions(ExternalFixtures):
    def test_another_workspace_sees_nothing(self):
        product_id, _plan = self._ready_product()
        [creative] = self._uploaded(product_id, _png((60, 60, 60)))
        other = self._make_context(role=ROLE_OWNER, actor="owner_b")
        self.addCleanup(cancel_workspace_jobs, other.workspace_id)
        foreign_upload = self._upload(_png((61, 61, 61)), ctx=other)
        self.current = other
        for response, code in (
            (self.client.get(f"/api/v1/external-creatives/{creative['external_creative_id']}"),
             "external_creative_not_found"),
            (self.client.post(f"/api/v1/external-creatives/{creative['external_creative_id']}/confirm",
                              json={"tags": []}, headers=self._headers()), "external_creative_not_found"),
            (self.client.get("/api/v1/external-creatives", params={"product_id": product_id}), "product_not_found"),
            (self.client.post("/api/v1/external-creatives", headers=self._headers(), json={
                "product_id": product_id, "upload_intent_ids": [foreign_upload], "rights": RIGHTS,
            }), "product_not_found"),
        ):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["code"], code)
        self.assertEqual(self.client.get("/api/v1/external-creatives").json()["items"], [])
        # This Workspace's product with another Workspace's upload: not found.
        self.current = self.ctx
        refused = self._submit(
            {"product_id": product_id, "upload_intent_ids": [foreign_upload], "rights": RIGHTS}, expect=404,
        )
        self.assertEqual(refused["code"], "upload_intent_not_found")

    def test_owners_and_members_confirm_but_a_system_identity_cannot(self):
        product_id, _plan = self._ready_product()
        [creative] = self._uploaded(product_id, _png((80, 10, 10)))
        self.current = self.owner_ctx(self.ctx)
        self.assertEqual(self._confirm(creative["external_creative_id"], {"tags": []})["status"], "confirmed")
        system = TenantContext(
            workspace_id=self.ctx.workspace_id, role="system", executor="system:worker",
            initiator_id="member_a", initiator_role="member",
        )
        self.current = system
        for response in (
            self.client.get(f"/api/v1/external-creatives/{creative['external_creative_id']}"),
            self.client.get("/api/v1/external-creatives"),
            self.client.post(f"/api/v1/external-creatives/{creative['external_creative_id']}/confirm",
                             json={"tags": []}, headers=self._headers()),
            self.client.post("/api/v1/external-creatives", headers=self._headers(), json={
                "product_id": product_id, "upload_intent_ids": ["ui_x"], "rights": RIGHTS,
            }),
        ):
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["code"], "external_creative_forbidden")


class TestPureHelpers(ExternalFixtures):
    def test_aspect_ratio_names_common_ad_ratios(self):
        self.assertEqual(aspect_ratio(1080, 1080), "1:1")
        self.assertEqual(aspect_ratio(1080, 1350), "4:5")
        self.assertEqual(aspect_ratio(1080, 1920), "9:16")
        self.assertEqual(aspect_ratio(1000, 700), "10:7")

    def test_the_mock_fixture_is_deterministic_and_schema_valid(self):
        from types import SimpleNamespace

        from app.providers import json_schema

        vocabulary = {"factors": [
            {"factor_id": "people", "label": "人物", "values": [{"value_id": "v1", "label": "有人"},
                                                               {"value_id": "v2", "label": "无人"}]},
        ]}
        schema = tagging.build_schema(vocabulary)
        request = SimpleNamespace(
            schema_name=tagging.SCHEMA_NAME, json_schema=schema,
            images=(SimpleNamespace(sha256="ab" * 32),), request_digest="x",
        )
        first = tagging._mock_asset_tagging(request)
        self.assertEqual(first, tagging._mock_asset_tagging(request))
        self.assertEqual(json_schema.validate(first, schema), [])
        normalized = tagging.normalize(first, vocabulary)
        self.assertEqual(normalized["factors"][0]["factor_id"], "people")
        self.assertEqual(tagging.normalize({"factors": {"f1": {"value_id": "none", "confidence": 3,
                                                               "reason": "x"}}}, vocabulary)["factors"][0],
                         {"factor_id": "people", "value_id": None, "confidence": 1.0, "reason": "x"})


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
