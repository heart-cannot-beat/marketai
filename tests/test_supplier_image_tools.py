"""Actual HTTP/preview/cost/Worker/version/review; only external transport is faked."""
import asyncio
import base64
import hashlib
import io
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from app import config
from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, JobRecord
from app.entitlements.cost_models import SpendReservationRecord
from app.generation.image_tools import ImageToolSpec, prepare_input, finish, delivery_metadata, ImageToolOutputError
from app.generation.models import RunItemRecord, GenerationRecipeSnapshotRecord
from app.generation.paid_execution import execute_run_item, asset_blob_ref
from app.marketing.blob_read import read_committed
from app.providers.adapter import ProviderCallRequest, ProviderCallResult, ProviderDispatchError
from app.providers.base import reference_delivery_metadata
from app.providers.openai_img import OpenAIImageProvider
from app.providers.gemini import GeminiProvider
from app.providers.seedream import SeedreamProvider
from tests.test_marketing_workflow import MarketingWorkflowFixtures


def picture(size=(80, 60), transparent=False, color=(200, 40, 25, 255)):
    image = Image.new("RGBA", size, color)
    if transparent:
        image.putpixel((0, 0), (0, 0, 0, 0))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def spec(kind, **values):
    return {"kind": kind, "source_width": 80, "source_height": 60,
        **({"scale": 2} if kind == "upscale" else {}),
        **({"region": {"x": .25, "y": .25, "width": .25, "height": .25}} if kind == "erase" else {}), **values}


@pytest.mark.parametrize("model", ["gpt-image-1.5", "gpt-image-1-mini", "gpt-image-2"])
def test_openai_native_mask_and_model_correct_fidelity(model):
    source, mask = prepare_input(picture(), spec("erase"))
    seen = []
    def handle(request):
        seen.append(request)
        assert request.url.path.endswith("/images/edits")
        assert source in request.content and mask in request.content
        assert b'name="mask"' in request.content
        assert b'name="output_format"\r\n\r\npng' in request.content
        assert (b'name="input_fidelity"' in request.content) == (model == "gpt-image-1.5")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(picture()).decode()}]})
    original = httpx.AsyncClient
    with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
        result = asyncio.run(OpenAIImageProvider().execute(ProviderCallRequest("Erase", (source,), "4:5", "2K", model, "one-edit", memoryview(b"test-only"), "erase", mask)))
    assert len(seen) == 1
    assert result.metadata["edit_mask_sha256"] == hashlib.sha256(mask).hexdigest()


def test_openai_transparent_background_is_a_wire_parameter():
    def handle(request):
        assert b'name="background"\r\n\r\ntransparent' in request.content
        assert b'name="output_format"\r\n\r\npng' in request.content
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(picture(transparent=True)).decode()}]})
    original = httpx.AsyncClient
    with patch("httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw)):
        result = asyncio.run(OpenAIImageProvider().execute(ProviderCallRequest("Cut out", (picture(),), "4:5", "2K", "gpt-image-1.5", "one-edit", None, "remove_background")))
    assert result.metadata["image_tool_operation"] == "remove_background"


@pytest.mark.parametrize("provider", [GeminiProvider(), SeedreamProvider()])
def test_unsupported_tools_never_silently_become_ordinary_generation(provider):
    with patch("httpx.AsyncClient") as network:
        with pytest.raises(ProviderDispatchError) as error:
            asyncio.run(provider.execute(ProviderCallRequest("Cut out", (picture(),), "4:5", "2K", provider.default_model, "blocked", None, "remove_background")))
    assert error.value.dispatch_certainty == "not_dispatched"
    network.assert_not_called()


def test_mask_polarity_and_outside_pixels_including_alpha_are_exact():
    source = picture(transparent=True)
    prepared, mask = prepare_input(source, spec("erase"))
    with Image.open(io.BytesIO(mask)) as image:
        assert image.getpixel((20, 15))[3] == 0
        assert image.getpixel((19, 15))[3] == 255
        assert image.getpixel((39, 29))[3] == 0
        assert image.getpixel((40, 29))[3] == 255
    result, evidence = finish(source, picture(color=(0, 255, 0, 255)), spec("erase"))
    before, after = Image.open(io.BytesIO(source)), Image.open(io.BytesIO(result))
    for y in range(60):
        for x in range(80):
            if 20 <= x < 40 and 15 <= y < 30:
                assert after.getpixel((x, y)) == (0, 255, 0, 255)
            else:
                assert after.getpixel((x, y)) == before.getpixel((x, y))
    assert evidence["outside_selection_preserved"]


@pytest.mark.parametrize("bad", [spec("upscale", scale=None), spec("erase", region=None), spec("erase", region={"x": .9, "y": 0, "width": .5, "height": 1}), spec("upscale", source_width=8192, source_height=8192), spec("erase", region={"x": 0, "y": 0, "width": .0001, "height": 1})])
def test_invalid_specs_rejected_before_any_job(bad):
    with pytest.raises(ValueError):
        ImageToolSpec.model_validate(bad)


@pytest.mark.parametrize("transparent_color", [(1, 2, 3, 255), (0, 0, 0, 0)])
def test_opaque_or_empty_cutout_cannot_become_a_success(transparent_color):
    with pytest.raises(ImageToolOutputError):
        finish(picture(), picture(color=transparent_color), spec("remove_background"))


class TestSupplierImageTools(MarketingWorkflowFixtures):
    def setup_round(self, paid=False):
        from tests.test_marketing_ai_qc import TestMarketingAIQC
        return TestMarketingAIQC.setup_round(self, paid=paid)

    def refresh(self, row):
        return self.client.get(f"/api/v1/marketing/rounds/{row['id']}?include_image_edits=true&include_version_history=true").json()["data"]

    def prepare(self, row, kind, **values):
        row = self.command(row, "feedback-drafts", {"asset_version_id": row["assets"][0]["asset_version_id"],
            "provider": "openai" if row["execution_mode"] == "byok" else "mock",
            "model": config.OPENAI_IMAGE_MODEL if row["execution_mode"] == "byok" else "mock-v1", "image_tool": spec(kind, **values)}, 201)
        draft = self.client.get(f"/api/v1/generation/drafts/{row['feedback_drafts'][-1]['draft_id']}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews", headers=self.headers(draft["revision"]))
        self.assertEqual(preview.status_code, 201, preview.text)
        return row, draft, preview.json()

    def submit(self, draft, preview):
        response = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
            json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]})
        self.assertEqual(response.status_code, 202, response.text)
        with tenant_session(self.context) as session:
            item = session.scalar(select(RunItemRecord).where(RunItemRecord.generation_run_id == response.json()["generation_run_id"]))
            job = session.scalar(select(JobRecord).where(JobRecord.resource_id == item.id))
            return item.id, job.id

    @staticmethod
    def adapter(output):
        async def execute(request):
            return ProviderCallResult(output, "test-transport-only", False,
                {**reference_delivery_metadata(request.reference_inputs), **delivery_metadata(request.edit_operation, request.edit_mask)})
        return AsyncMock(execute=AsyncMock(side_effect=execute))

    def test_three_tools_freeze_preview_charge_once_and_require_fresh_review(self):
        _, row = self.setup_round(paid=True)
        source_id = row["assets"][0]["asset_version_id"]
        row = self.command(row, "reviews", {"asset_version_id":source_id, "quality":"approved",
            "checks": {k:"pass" for k in row["assets"][0]["required_checks"]}, "note":"Original fixture review"}, 201)
        for kind in ("upscale", "remove_background", "erase"):
            with self.subTest(kind=kind):
                row, draft, preview = self.prepare(row, kind)
                self.assertEqual(preview["image_tool"]["kind"], kind)
                self.assertEqual(preview["maximum_cost"]["amount"], "0.50")
                self.assertEqual(preview["format_config"]["image_size"], "2K")
                item_id, job_id = self.submit(draft, preview)
                provider = self.adapter(picture(transparent=kind == "remove_background"))
                asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=provider))
                asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=provider))
                self.assertEqual(provider.execute.await_count, 1)
                self.assertEqual(provider.execute.call_args.args[0].edit_operation, kind)
                row = self.refresh(row)
                with tenant_session(self.context) as session:
                    item = session.get(RunItemRecord, item_id)
                    self.assertEqual(item.state, "succeeded")
                    version = session.scalar(select(AssetVersionRecord).where(AssetVersionRecord.producing_run_item_id == item_id))
                    child_id = version.id
                    blob = asset_blob_ref(session, self.context, version.id)
                    self.assertEqual(version.parent_version_id, source_id)
                    reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
                    self.assertEqual(reservation.state, "settled")
                content = asyncio.run(read_committed(self.blobs, blob, 1000000))
                self.assertEqual(Image.open(io.BytesIO(content)).size, (160,120) if kind == "upscale" else (80,60))
                child = next(a for a in row["assets"] if a["asset_version_id"] == child_id)
                self.assertIsNone(child["quality"])
                self.assertIsNone(child["ai_qc"])
                self.assertFalse(child["ready"])
                row = self.command(row, "reviews", {"asset_version_id":child_id, "quality":"approved",
                    "checks": {k:"pass" for k in child["required_checks"]}, "note":"Fresh edited-version fixture review"}, 201)
                self.assertTrue(next(a for a in row["assets"] if a["asset_version_id"] == child_id)["ready"])

    def test_wrong_source_dimensions_release_budget_without_dispatch(self):
        _, row = self.setup_round(paid=True)
        row, draft, preview = self.prepare(row, "erase", source_width=81)
        item_id, job_id = self.submit(draft, preview)
        provider = self.adapter(picture())
        with self.assertRaises(ValueError):
            asyncio.run(execute_run_item(self.context.for_system_execution(), job_id, item_id, self.blobs, adapter=provider))
        provider.execute.assert_not_awaited()
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(RunItemRecord, item_id).state, "failed")
            self.assertEqual(session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id)).state, "released")

    def test_invalid_cutout_is_failed_but_paid_call_is_settled_without_retry(self):
        _, row = self.setup_round(paid=True)
        row, draft, preview = self.prepare(row, "remove_background")
        item_id, job_id = self.submit(draft, preview)
        provider = self.adapter(picture())
        for _ in range(2):
            asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=provider))
        self.assertEqual(provider.execute.await_count, 1)
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(RunItemRecord, item_id).state, "failed")
            self.assertEqual(session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id)).state, "settled")

    def test_tool_format_or_provider_changes_require_preparing_a_new_draft(self):
        _, row = self.setup_round()
        row, draft, _ = self.prepare(row, "upscale")
        from app.generation.draft import update_draft
        with tenant_session(self.context) as session:
            updated = update_draft(session, self.context, draft["id"], expected_revision=draft["revision"], format_config={"aspect_ratio":"4:5", "image_size":"1K", "variants":2})
            revision = updated.revision
        response = self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews", headers=self.headers(revision))
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "image_tool_format_changed")

    def test_changed_selection_has_new_preview_and_pending_call_is_never_resent(self):
        _, row = self.setup_round(paid=True)
        row, first, first_preview = self.prepare(row, "erase")
        row, second, second_preview = self.prepare(row, "erase", region={"x":.5,"y":.25,"width":.25,"height":.25})
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first_preview["preview_hash"], second_preview["preview_hash"])
        item_id, job_id = self.submit(second, second_preview)
        provider = AsyncMock()
        provider.execute.side_effect = ProviderDispatchError("uncertain", "fixture timeout")
        for _ in range(2):
            asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=provider))
        self.assertEqual(provider.execute.await_count, 1)
        with tenant_session(self.context) as session:
            self.assertEqual(session.get(RunItemRecord, item_id).state, "uncertain")
            self.assertEqual(session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id)).state, "uncertain")

    def test_tool_parameters_remain_in_the_reviewed_export_and_original_is_unchanged(self):
        _, row = self.setup_round()
        source = row["assets"][0]["asset_version_id"]
        with tenant_session(self.context) as session:
            original_blob = asset_blob_ref(session, self.context, source)
        original = asyncio.run(read_committed(self.blobs, original_blob, 100000))
        row, draft, preview = self.prepare(row, "erase")
        item_id, job_id = self.submit(draft, preview)
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=self.adapter(picture())))
        row = self.refresh(row)
        child = next(a for a in row["assets"] if a["parent_version_id"] == source)
        selected = [a for a in row["assets"] if a["asset_version_id"] != source]
        for asset in selected:
            row = self.command(row, "reviews", {"asset_version_id":asset["asset_version_id"], "quality":"approved",
                "checks":{k:"pass" for k in asset["required_checks"]}, "note":"Fresh selected-version fixture review"}, 201)
        self.command(row, "handoff", {"asset_version_ids":[a["asset_version_id"] for a in selected]}, 202)
        row = self.refresh(row)
        entry = next(e for e in row["handoff"]["entries"] if e["asset_version_id"] == child["asset_version_id"])
        self.assertEqual(entry["provenance"]["image_tool"]["kind"], "erase")
        self.assertEqual(entry["provenance"]["image_tool"]["region"]["x"], .25)
        self.assertEqual(entry["provenance"]["source_asset_version_id"], source)
        self.assertEqual(original, asyncio.run(read_committed(self.blobs, original_blob, 100000)))

    def test_capabilities_offer_only_native_tools_with_the_workspace_quotes(self):
        self.paid_setup()
        catalog = self.client.get("/api/v1/generation/capabilities").json()["provider_options"]
        openai = next(p for p in catalog if p["id"] == "openai")
        self.assertIn("remove_background", next(m for m in openai["models"] if m["id"] == "gpt-image-1.5")["image_tools"])
        self.assertNotIn("remove_background", next(m for m in openai["models"] if m["id"] == "gpt-image-2")["image_tools"])
        for name in ("gemini", "seedream"):
            self.assertTrue(all(m["image_tools"] == ["upscale"] for m in next(p for p in catalog if p["id"] == name)["models"]))
