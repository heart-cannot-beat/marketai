"""Brush coordinates, mask transport, immutable lineage and fresh reviews."""
import asyncio
import io
from unittest.mock import patch

import pytest
from PIL import Image
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetVersionRecord
from app.generation.image_tools import ImageToolSpec, finish, prepare_input, selection_mask
from app.generation.models import RunItemRecord
from app.generation.paid_execution import execute_run_item
from app.marketing.service import digest
from tests import test_supplier_image_tools as supplier
from tests import test_marketing_ai_qc as qc_fixtures
from tests.test_marketing_workflow import MarketingWorkflowFixtures
picture, spec = supplier.picture, supplier.spec


def brush(**overrides):
    values = spec("erase", region=None, strokes=[
        {"width": .1, "points": [{"x": 0, "y": 0}, {"x": .5, "y": .5}]},
        {"width": .2, "points": [{"x": 1, "y": 1}]},
    ])
    values.update(overrides)
    return values


def test_brush_mask_and_result_have_identical_selection_and_preserve_rgba():
    source = picture(transparent=True)
    tool = ImageToolSpec.model_validate(brush())
    mask = selection_mask(tool, (80, 60))
    prepared, native_mask = prepare_input(source, tool)
    assert Image.open(io.BytesIO(prepared)).size == (80, 60)
    alpha = Image.open(io.BytesIO(native_mask)).getchannel("A")
    output, _ = finish(source, picture(color=(0, 255, 0, 255)), tool)
    before, after = Image.open(io.BytesIO(source)), Image.open(io.BytesIO(output))
    assert mask.getpixel((0, 0)) == mask.getpixel((79, 59)) == 255
    assert mask.getpixel((79, 0)) == 0
    for y in range(60):
        for x in range(80):
            assert alpha.getpixel((x, y)) == 255 - mask.getpixel((x, y))
            original = before.getpixel((x, y))
            assert after.getpixel((x, y)) == ((0, 255, 0, original[3]) if mask.getpixel((x, y)) else original)
    assert selection_mask(tool, (80, 60)).tobytes() == mask.tobytes()
    assert digest(brush()) != digest(brush(strokes=[{"width": .11, "points": [{"x": 0, "y": 0}]}]))


@pytest.mark.parametrize("size", [(1, 1), (80, 60), (8192, 1)])
def test_minimum_brush_tap_always_selects_a_pixel(size):
    tool = ImageToolSpec(kind="erase", source_width=size[0], source_height=size[1],
        strokes=[{"width": .002, "points": [{"x": 1, "y": 1}]}])
    mask = selection_mask(tool, size)
    assert mask.getbbox() == (size[0] - 1, size[1] - 1, size[0], size[1])


def test_existing_rectangular_tools_do_not_gain_an_unknown_null_strokes_field():
    # The shipped Web's strict ImageToolSpec rejects unknown response keys.
    assert "strokes" not in ImageToolSpec.model_validate(spec("erase")).model_dump(mode="json")


@pytest.mark.parametrize("values", [
    {"strokes": []}, {"region": {"x": 0, "y": 0, "width": .5, "height": .5}},
    {"strokes": [{"width": .1, "points": []}]},
    {"strokes": [{"width": .1, "points": [{"x": 1.01, "y": 0}]}]},
    {"strokes": [{"width": .1, "points": [{"x": float("nan"), "y": 0}]}]},
    {"strokes": [{"width": .001, "points": [{"x": 0, "y": 0}]}]},
    {"strokes": [{"width": .1, "points": [{"x": 0, "y": 0}] * 513}]},
    {"strokes": [{"width": .1, "points": [{"x": 0, "y": 0}]}] * 129},
    {"strokes": [{"width": .1, "points": [{"x": 0, "y": 0}] * 512}] * 17},
    {"kind": "remove_background"},
])
def test_unbounded_or_ambiguous_brushes_are_rejected(values):
    body = brush()
    body.update(values)
    with pytest.raises(ValueError):
        ImageToolSpec.model_validate(body)


class TestBrushWorkflow(MarketingWorkflowFixtures):
    setup_round = supplier.TestSupplierImageTools.setup_round
    refresh = supplier.TestSupplierImageTools.refresh
    prepare = supplier.TestSupplierImageTools.prepare
    submit = supplier.TestSupplierImageTools.submit

    def test_shipped_image_edit_opt_in_stays_compatible_until_history_is_requested(self):
        project, row = self.setup_round()
        row, _, preview = self.prepare(row, "erase")
        assert "strokes" not in row["feedback_drafts"][-1]["image_tool"]
        assert "strokes" not in preview["image_tool"]
        legacy_keys = {"parent_version_id", "asset_version_id", "combination_id", "headline", "body_copy",
            "required_checks", "quality", "ready", "mime_type", "ai_qc"}
        for path, key in ((f"/api/v1/marketing/rounds/{row['id']}", "data"),
                          (f"/api/v1/marketing/projects/{project['id']}/workflow", "current")):
            old = self.client.get(path, params={"include_image_edits": True}).json()[key]
            assert old["assets"]
            assert all(set(a) <= legacy_keys for a in old["assets"]), "Old Web rejects new history fields during API-first rollout"
            history = self.client.get(path, params={"include_image_edits": True, "include_version_history": True}).json()[key]
            assert all(a["asset_id"] and a["created_at"] and a["edit_operation"] for a in history["assets"])

    def test_mock_brush_from_an_older_version_freezes_points_and_needs_fresh_review(self):
        _, row = self.setup_round()
        source_id = row["assets"][0]["asset_version_id"]
        row = self.command(row, "reviews", {"asset_version_id": source_id, "quality": "approved",
            "checks": {k: "pass" for k in row["assets"][0]["required_checks"]}, "note": "Source review"}, 201)
        before = self.refresh(row)
        original = next(a for a in before["assets"] if a["asset_version_id"] == source_id)
        child_ids = []
        for points in ([{"x": .25, "y": .25}, {"x": .5, "y": .5}], [{"x": .75, "y": .75}]):
            row, draft, preview = self.prepare(row, "erase", region=None, strokes=[{"width": .1, "points": points}])
            assert preview["execution_mode"] == "mock"
            assert preview["maximum_cost"]["amount"] == "0"
            assert preview["image_tool"]["strokes"][0]["points"] == points
            item_id, job_id = self.submit(draft, preview)
            # The built-in Mock executes the real finishing/persistence path.
            with patch("httpx.AsyncClient", side_effect=AssertionError("No external network")):
                asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs))
            with tenant_session(self.context) as session:
                item = session.get(RunItemRecord, item_id)
                assert item.state == "succeeded"
                version = session.scalar(select(AssetVersionRecord).where(AssetVersionRecord.producing_run_item_id == item_id))
                assert version.parent_version_id == source_id
                child_ids.append(version.id)
            row = self.refresh(row)
            child = next(a for a in row["assets"] if a["asset_version_id"] == child_ids[-1])
            assert child["asset_id"] == original["asset_id"]
            assert child["created_at"] and child["edit_operation"] == "erase"
            assert child["quality"] is None and child["ai_qc"] is None and not child["ready"]
            quote = qc_fixtures.TestMarketingAIQC.quote(self, row)
            assert quote["execution_mode"] == "mock" and quote["maximum_cost"] == "0"
            row = self.command(row, "ai-qc", {"quote_hash": quote["quote_hash"]}, 202)
            with patch("httpx.AsyncClient", side_effect=AssertionError("No external vision")):
                qc_fixtures.TestMarketingAIQC.execute_checks(self, row)
            row = self.refresh(row)
            child = next(a for a in row["assets"] if a["asset_version_id"] == child_ids[-1])
            ai = child["ai_qc"]
            assert ai["state"] == "succeeded" and ai["result"]["source"] == "controlled_mock"
            assert child["quality"] is None and not child["ready"]
            row = self.command(row, "reviews", {"asset_version_id": child["asset_version_id"], "quality": "approved",
                "checks": {k: "pass" for k in child["required_checks"]}, "note": "Fresh brush result review",
                "ai_qc_id": ai["id"], "compared_asset_version_ids": ai["compared_asset_version_ids"],
                "override_note": "Inspected exact local fixture pixels; Mock suggestions do not certify image quality."}, 201)
        final = self.refresh(row)
        assert next(a for a in final["assets"] if a["asset_version_id"] == source_id) == original
        assert len(set(child_ids)) == 2
        assert all(next(a for a in final["assets"] if a["asset_version_id"] == i)["ready"] for i in child_ids)
        legacy = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
        assert all(not ({"edit_operation", "created_at", "asset_id"} & a.keys()) for a in legacy["assets"])
