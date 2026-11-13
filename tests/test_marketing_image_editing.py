"""Actual local pixels + native HTTP/worker/review, with only AI transport faked."""
import asyncio
import io
from unittest.mock import AsyncMock, patch

from PIL import Image
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord, JobRecord
from app.entitlements.cost_models import SpendReservationRecord
from app.generation.models import RunItemRecord
from app.generation.paid_execution import execute_run_item, asset_blob_ref
from app.marketing.image_editing import execute_edit, transform
from app.marketing.blob_read import read_committed
from app.marketing.workflow_models import MarketingImageEditRecord
from app.providers.adapter import ProviderCallResult
from tests.support.library_fixtures import png_bytes
from tests.support.storage_limit import set_storage_headroom
from tests.test_marketing_workflow import MarketingWorkflowFixtures


class TestMarketingImageEditing(MarketingWorkflowFixtures):
    def setup_images(self):
        project = self.confirmed()
        self.generate(project)
        return project, self.start_round(project)

    def run_edit(self, edit_id):
        asyncio.run(execute_edit(self.context, edit_id, self.blobs))

    def refresh(self, row):
        return self.client.get(f"/api/v1/marketing/rounds/{row['id']}", params={"include_image_edits": True}).json()["data"]

    def test_crop_then_rotation_creates_child_and_reenters_review_and_export(self):
        _, row = self.setup_images()
        source = row["assets"][0]
        row = self.command(row, "reviews", {"asset_version_id": source["asset_version_id"], "quality": "approved",
            "checks": {k: "pass" for k in source["required_checks"]}, "note": "Review original"}, 201)
        body = {"asset_version_id": source["asset_version_id"], "crop": {"x": 0, "y": 0, "width": .5, "height": 1}, "rotation": 90}
        before = row
        row = self.command(row, "image-edits", body, 202, key="crop-once")
        replay = self.command(before, "image-edits", body, 202, key="crop-once")
        self.assertEqual(row["image_edits"], replay["image_edits"])
        edit_id = row["image_edits"][0]["id"]
        self.run_edit(edit_id)
        self.run_edit(edit_id)
        row = self.refresh(row)
        edit = row["image_edits"][0]
        self.assertEqual(edit["state"], "succeeded")
        child = next(a for a in row["assets"] if a["asset_version_id"] == edit["result_asset_version_id"])
        self.assertEqual(child["parent_version_id"], source["asset_version_id"])
        self.assertIsNone(child["quality"])
        self.assertFalse(child["ready"])
        with tenant_session(self.context) as session:
            version = session.get(AssetVersionRecord, child["asset_version_id"])
            self.assertEqual(version.origin_kind, "edit")
            self.assertIsNone(version.provider_attempt_id)
            blob = asset_blob_ref(session, self.context, version.id)
            # The edited image gets its own board/gallery derivative, never the parent's.
            self.assertIn("/generation_thumbnail/", version.thumbnail_object_key)
            self.assertEqual((version.thumbnail_mime_type, version.thumbnail_width, version.thumbnail_height), ("image/jpeg", 60, 40))
            self.assertNotEqual(version.thumbnail_object_key, session.get(AssetVersionRecord, source["asset_version_id"]).thumbnail_object_key)
        pixels = asyncio.run(read_committed(self.blobs, blob, 100000))
        self.assertEqual(Image.open(io.BytesIO(pixels)).size, (60, 40))
        selected = [a for a in row["assets"] if a["asset_version_id"] != source["asset_version_id"]]
        for asset in selected:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Fresh exact version review"}, 201)
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in selected]}, 202)
        row = self.refresh(row)
        entry = next(e for e in row["handoff"]["entries"] if e["asset_version_id"] == child["asset_version_id"])
        self.assertEqual(entry["provenance"]["kind"], "image_edit")
        self.assertEqual(entry["provenance"]["rotation"], 90)

    def test_noop_does_not_create_execution_or_inherit_new_approval(self):
        _, row = self.setup_images()
        row = self.command(row, "image-edits", {"asset_version_id": row["assets"][0]["asset_version_id"]}, 202)
        self.run_edit(row["image_edits"][0]["id"])
        after = self.refresh(row)
        self.assertEqual(len(after["assets"]), len(row["assets"]))
        self.assertEqual(after["image_edits"][0]["state"], "unchanged")

    def test_new_projection_is_opt_in_for_old_strict_bff_clients(self):
        _, row = self.setup_images()
        row = self.command(row, "image-edits", {"asset_version_id": row["assets"][0]["asset_version_id"], "rotation": 90}, 202)
        self.run_edit(row["image_edits"][0]["id"])
        legacy = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
        self.assertNotIn("image_edits", legacy)
        self.assertTrue(all("parent_version_id" not in a for a in legacy["assets"]))
        current = self.refresh(row)
        self.assertEqual(current["image_edits"][0]["state"], "succeeded")
        self.assertTrue(any(a["parent_version_id"] for a in current["assets"]))
        asset = current["assets"][0]
        reviewed = self.command(current, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
            "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Legacy response compatibility"}, 201)
        self.assertNotIn("image_edits", reviewed)
        self.assertTrue(all("parent_version_id" not in a for a in reviewed["assets"]))

    def test_invalid_and_foreign_source_rejected_before_job(self):
        _, row = self.setup_images()
        for crop in ({"x": .8, "y": 0, "width": .5, "height": 1}, {"x": 0, "y": 0, "width": 0, "height": 1}):
            self.command(row, "image-edits", {"asset_version_id": row["assets"][0]["asset_version_id"], "crop": crop}, 422)
        _, other = self.setup_images()
        error = self.command(row, "image-edits", {"asset_version_id": other["assets"][0]["asset_version_id"]}, 404)
        self.assertEqual(error["code"], "asset_not_in_round")

    def test_storage_at_the_limit_refuses_a_new_edit_but_not_a_replay(self):
        # #158: an image edit stores a new version, so it is a generation.
        _, row = self.setup_images()
        source = row["assets"][0]["asset_version_id"]
        body = {"asset_version_id": source, "rotation": 90}
        admitted = self.command(row, "image-edits", body, 202, key="rotate-before-full")
        self.assertGreater(set_storage_headroom(self.context, 1), 0)  # one byte below: still admitted
        admitted = self.command(admitted, "image-edits", {"asset_version_id": source, "rotation": 180}, 202)

        set_storage_headroom(self.context, 0)
        refused = self.command(admitted, "image-edits", {"asset_version_id": source, "rotation": 270}, 409)
        self.assertEqual(refused["code"], "storage_limit_reached")
        self.assertIn("limit_gib", refused["params"])
        replay = self.command(row, "image-edits", body, 202, key="rotate-before-full")
        self.assertEqual(len(replay["image_edits"]), 2)  # the replay, and nothing new
        self.assertEqual(len(self.refresh(row)["image_edits"]), 2)

    def test_storage_at_the_limit_refuses_a_marketing_generation_run(self):
        # The marketing drafts submit through the selected-recipe submission.
        project = self.confirmed()
        drafts = self.generate(project, count=0)
        set_storage_headroom(self.context, 0)
        draft = self.client.get(f"/api/v1/generation/drafts/{drafts[0]}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{drafts[0]}/previews",
                                   headers=self.headers(draft["revision"])).json()
        body = {"preview_id": preview["id"], "preview_hash": preview["preview_hash"]}
        refused = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]), json=body)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["code"], "storage_limit_reached")

        set_storage_headroom(self.context, 1)
        admitted = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]), json=body)
        self.assertEqual(admitted.status_code, 202, admitted.text)

    def test_worker_refuses_edit_after_handoff_freezes_round(self):
        _, row = self.setup_images()
        source = row["assets"][0]["asset_version_id"]
        row = self.command(row, "image-edits", {"asset_version_id": source, "rotation": 90}, 202)
        pending_id = row["image_edits"][0]["id"]
        for asset in row["assets"]:
            row = self.command(row, "reviews", {"asset_version_id": asset["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in asset["required_checks"]}, "note": "Original approved"}, 201)
        row = self.command(row, "handoff", {"asset_version_ids": [a["asset_version_id"] for a in row["assets"]]}, 202)
        with self.assertRaises(ValueError):
            self.run_edit(pending_id)
        self.assertEqual(len(self.refresh(row)["assets"]), 5)

    def test_ai_edit_uses_exact_original_and_preserves_parent(self):
        _, row = self.setup_images()
        source = row["assets"][0]["asset_version_id"]
        row = self.command(row, "image-edits", {"asset_version_id": source,
            "crop": {"x": 0, "y": 0, "width": .5, "height": 1}, "rotation": 90}, 202)
        self.run_edit(row["image_edits"][0]["id"])
        row = self.refresh(row)
        source = row["image_edits"][0]["result_asset_version_id"]
        row = self.command(row, "feedback-drafts", {"asset_version_id": source, "note": "Keep the product, change the background",
            "provider": "mock", "model": "mock-v1"}, 201)
        draft = self.client.get(f"/api/v1/generation/drafts/{row['feedback_drafts'][0]['draft_id']}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews", headers=self.headers(draft["revision"])).json()
        run = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
            json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]})
        self.assertEqual(run.status_code, 202, run.text)
        with tenant_session(self.context) as session:
            item = session.scalar(select(RunItemRecord).where(RunItemRecord.generation_run_id == run.json()["generation_run_id"]))
            item_id = item.id
            job_id = session.scalar(select(JobRecord.id).where(JobRecord.resource_id == item.id))
            original = session.get(AssetVersionRecord, source)
            blob = asset_blob_ref(session, self.context, source)
        original_bytes = asyncio.run(read_committed(self.blobs, blob, 100000))
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(png_bytes(colour=(130, 45, 12)), "edit", False)
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        self.assertEqual(adapter.execute.await_count, 1)
        self.assertEqual(adapter.execute.call_args.args[0].reference_inputs, (original_bytes,))
        after = self.refresh(row)
        child = next(a for a in after["assets"] if a["parent_version_id"] == source)
        self.assertFalse(child["ready"])

    def test_pixel_rotation_direction_and_alpha_are_preserved(self):
        image = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
        image.putpixel((0, 0), (255, 0, 0, 128))
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        output, size = transform(buf.getvalue(), {"crop": {"x": 0, "y": 0, "width": .5, "height": 1}, "rotation": 90})
        result = Image.open(io.BytesIO(output))
        self.assertEqual(size, (2, 2))
        self.assertEqual(result.getpixel((1, 0)), (255, 0, 0, 128))
        self.assertEqual(result.getpixel((0, 1)), (0, 0, 0, 0))

    def test_native_worker_completes_the_persisted_local_job_and_operation(self):
        from app.worker_runtime import WorkerRuntime
        from app.platform.operations import get_operation
        project, row = self.setup_images()
        row = self.command(row, "image-edits", {"asset_version_id": row["assets"][0]["asset_version_id"], "rotation": 90}, 202)
        edit = row["image_edits"][0]
        path = f"/api/v1/marketing/projects/{project['id']}/workflow?include_polling=true&include_image_edits=true"
        self.assertEqual(self.client.get(path).json()["poll_after_ms"], 3000)
        runtime = WorkerRuntime(owner="image-edit-test")
        runtime.blob_store = self.blobs
        # Earlier fixtures may leave completed domain records with queued jobs;
        # drain only this job type through the actual dispatcher.
        for _ in range(20):
            asyncio.run(runtime.run_once(job_types=("marketing.image_edit",)))
            if self.refresh(row)["image_edits"][0]["state"] == "succeeded":
                break
        with tenant_session(self.context) as session:
            job = session.scalar(select(JobRecord).where(JobRecord.resource_id == edit["id"]))
            self.assertEqual(job.state, "succeeded")
            self.assertEqual(get_operation(session, edit["operation_id"]).state, "succeeded")
            self.assertFalse(session.scalars(select(SpendReservationRecord).where(SpendReservationRecord.job_id == job.id)).all())

        self.assertEqual(self.client.get(path).json()["poll_after_ms"], 0)

    def test_paid_edit_source_read_failure_releases_reservation_without_dispatch(self):
        from app import config
        from tests.test_marketing_ai_qc import TestMarketingAIQC
        _, row = TestMarketingAIQC.setup_round(self, paid=True)
        row = self.command(row, "feedback-drafts", {"asset_version_id": row["assets"][0]["asset_version_id"],
            "note": "Change the background", "provider": "openai", "model": config.OPENAI_IMAGE_MODEL}, 201)
        draft = self.client.get(f"/api/v1/generation/drafts/{row['feedback_drafts'][0]['draft_id']}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{draft['id']}/previews", headers=self.headers(draft["revision"])).json()
        response = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
            json={"preview_id": preview["id"], "preview_hash": preview["preview_hash"]})
        self.assertEqual(response.status_code, 202, response.text)
        with tenant_session(self.context) as session:
            item = session.scalar(select(RunItemRecord).where(RunItemRecord.generation_run_id == response.json()["generation_run_id"]))
            item_id = item.id
            job_id = session.scalar(select(JobRecord.id).where(JobRecord.resource_id == item.id))
        adapter, unavailable = AsyncMock(), AsyncMock()
        unavailable.open_committed = lambda ref: (_ for _ in ()).throw(OSError("missing source"))
        with self.assertRaises(Exception):
            asyncio.run(execute_run_item(self.context.for_system_execution(), job_id, item_id, unavailable, adapter=adapter))
        adapter.execute.assert_not_awaited()
        with tenant_session(self.context) as session:
            reservation = session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.run_item_id == item_id))
            self.assertEqual(reservation.state, "released")
            self.assertEqual(session.get(RunItemRecord, item_id).state, "failed")
