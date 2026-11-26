"""#268: an image the user asked for in 4:5 is delivered in 4:5, also when GPT-Image renders only 2:3.

GPT-Image renders 1024x1024, 1024x1536 and 1536x1024. A 4:5 request is sent at 1024x1536 (priced as
such); the Worker tells the model the frame, then keeps the central 1024x1280 and records the provider's
own size and bytes. Other providers, other ratios and image-tool edits (which already return the source's
size) are unchanged.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import unittest
from unittest.mock import AsyncMock

from PIL import Image
from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetRecord, AssetVersionRecord
from app.generation import canvas_fit
from app.generation.models import ProviderAttemptEventRecord, RunItemAssetVersionBindingRecord
from app.generation.paid_execution import asset_blob_ref, dispatch_prompt, execute_run_item
from app.providers.adapter import ProviderCallResult
from tests import test_generation_selection as selection
from tests.support.library_fixtures import LibraryFixtures

TOP, MIDDLE, BOTTOM = (200, 30, 30), (30, 160, 60), (30, 60, 200)


def striped(size=(1024, 1536), band=128, fmt="PNG", mode="RGB"):
    """A provider canvas whose top and bottom ``band`` rows differ from the middle."""
    width, height = size
    image = Image.new(mode, size, MIDDLE + ((255,) if mode == "RGBA" else ()))
    image.paste(TOP + ((255,) if mode == "RGBA" else ()), (0, 0, width, band))
    image.paste(BOTTOM + ((255,) if mode == "RGBA" else ()), (0, height - band, width, height))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def opened(content):
    image = Image.open(io.BytesIO(content))
    image.load()
    return image


class TestPlan(unittest.TestCase):
    def test_only_gpt_image_in_4_5_without_an_image_tool_is_fitted(self):
        fit = canvas_fit.plan("openai", "4:5")
        self.assertEqual((fit.canvas, fit.size), ((1024, 1536), (1024, 1280)))
        for provider, ratio in (("openai", "1:1"), ("openai", "9:16"), ("openai", "16:9"),
                                ("gemini", "4:5"), ("seedream", "4:5"), ("mock", "4:5"), (None, "4:5")):
            self.assertIsNone(canvas_fit.plan(provider, ratio), (provider, ratio))
        # An image tool's output is resized to its source by ``image_tools.finish``.
        self.assertIsNone(canvas_fit.plan("openai", "4:5", image_tool={"kind": "upscale"}))

    def test_the_frame_note_names_the_central_area_to_keep_text_and_product_in(self):
        note = canvas_fit.note(canvas_fit.plan("openai", "4:5"))
        for words in ("4:5", "1024x1536", "1024x1280", "128 px", "headline", "body text", "logo", "product"):
            self.assertIn(words, note)


class TestFit(unittest.TestCase):
    def test_a_2_3_canvas_keeps_its_central_4_5_and_records_the_providers_own_output(self):
        raw = striped()
        content, evidence = canvas_fit.fit(raw, canvas_fit.plan("openai", "4:5"))
        image = opened(content)
        self.assertEqual((image.format, image.size), ("PNG", (1024, 1280)))
        self.assertEqual({colour for _, colour in image.getcolors()}, {MIDDLE})
        self.assertEqual(evidence, {"policy": canvas_fit.POLICY, "aspect_ratio": "4:5",
                                    "provider_width": 1024, "provider_height": 1536, "width": 1024, "height": 1280,
                                    "provider_output_sha256": hashlib.sha256(raw).hexdigest()})

    def test_format_and_transparency_are_kept(self):
        jpeg, _ = canvas_fit.fit(striped(fmt="JPEG"), canvas_fit.plan("openai", "4:5"))
        self.assertEqual((opened(jpeg).format, opened(jpeg).size), ("JPEG", (1024, 1280)))
        rgba, _ = canvas_fit.fit(striped(mode="RGBA"), canvas_fit.plan("openai", "4:5"))
        self.assertEqual((opened(rgba).mode, opened(rgba).size), ("RGBA", (1024, 1280)))

    def test_an_output_already_in_4_5_is_kept_byte_for_byte(self):
        raw = striped(size=(1024, 1280), band=0)
        content, evidence = canvas_fit.fit(raw, canvas_fit.plan("openai", "4:5"))
        self.assertEqual(content, raw)
        self.assertEqual((evidence["provider_height"], evidence["height"]), (1280, 1280))

    def test_a_wider_canvas_is_cropped_at_the_sides(self):
        content, evidence = canvas_fit.fit(striped(size=(1536, 1024), band=0), canvas_fit.plan("openai", "4:5"))
        self.assertEqual(opened(content).size, (819, 1024))
        self.assertEqual((evidence["width"], evidence["height"]), (819, 1024))


class TestDispatchPrompt(unittest.TestCase):
    FROZEN = {"product": {"content": {"name": "Acme Charge 65W"}}, "prompt": "A charger on a desk.",
              "format": {"aspect_ratio": "4:5", "variants": 1}}

    def test_gpt_image_in_4_5_is_told_the_frame_before_the_variation_line(self):
        prompt = dispatch_prompt(self.FROZEN, 0, "openai")
        note = canvas_fit.note(canvas_fit.plan("openai", "4:5"))
        self.assertIn("A charger on a desk.\n" + note + "\nCreative variation 1 of 1.\n\n", prompt)

    def test_other_providers_and_image_tools_get_the_prompt_as_before(self):
        before = dispatch_prompt(self.FROZEN, 0)
        for provider in ("gemini", "seedream", "mock"):
            self.assertEqual(dispatch_prompt(self.FROZEN, 0, provider), before)
        self.assertNotIn("1024x1280", before)
        tool = {**self.FROZEN, "image_tool": {"kind": "upscale", "scale": 2, "source_width": 10, "source_height": 10}}
        self.assertNotIn("1024x1280", dispatch_prompt(tool, 0, "openai"))


class TestWorkerDelivers4x5(LibraryFixtures):
    """The HTTP draft -> preview -> run -> Worker path; only the provider adapter is faked."""

    setUp = selection.TestGenerationSelection.setUp
    headers = selection.TestGenerationSelection.headers
    paid_setup = selection.TestGenerationSelection.paid_setup
    draft = selection.TestGenerationSelection.draft
    preview = selection.TestGenerationSelection.preview
    submit = selection.TestGenerationSelection.submit
    items = selection.TestGenerationSelection.items

    def run_one(self, provider, output):
        draft = self.draft(provider)
        result = self.submit(draft, self.preview(draft))
        self.assertEqual(result.status_code, 202, result.text)
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(output, "external-request", False)
        [(item_id, job_id)] = self.items(result.json()["generation_run_id"])
        asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        with tenant_session(self.context) as session:
            binding = session.scalar(select(RunItemAssetVersionBindingRecord).where(
                RunItemAssetVersionBindingRecord.workspace_id == self.context.workspace_id,
                RunItemAssetVersionBindingRecord.run_item_id == item_id))
            version = session.get(AssetVersionRecord, binding.asset_version_id)
            events = {event.event_type: event.payload_json for event in session.scalars(select(ProviderAttemptEventRecord).where(
                ProviderAttemptEventRecord.workspace_id == self.context.workspace_id,
                ProviderAttemptEventRecord.provider_call_attempt_id == binding.producing_provider_attempt_id))}
            stored = asyncio.run(self.blobs.read_committed(asset_blob_ref(session, self.context, version.id), 20 * 1024 * 1024))
            asset_prompt = session.get(AssetRecord, version.asset_id).prompt
            return {"version": (version.aspect_ratio, version.content_sha256), "events": events, "stored": stored,
                    "sent": adapter.execute.await_args.args[0], "asset_prompt": asset_prompt}

    def test_gpt_image_4_5_is_stored_as_4_5_with_the_providers_size_and_hash_as_evidence(self):
        self.paid_setup()
        raw = striped()
        ran = self.run_one("openai", raw)
        image = opened(ran["stored"])
        self.assertEqual(image.size, (1024, 1280))
        self.assertEqual({colour for _, colour in image.getcolors()}, {MIDDLE})
        self.assertEqual(ran["version"], ("4:5", hashlib.sha256(ran["stored"]).hexdigest()))
        self.assertEqual(ran["events"]["dispatch_started"]["canvas_fit"], canvas_fit.POLICY)
        self.assertEqual(ran["events"]["response_received"]["canvas_fit"], {
            "policy": canvas_fit.POLICY, "aspect_ratio": "4:5", "provider_width": 1024, "provider_height": 1536,
            "width": 1024, "height": 1280, "provider_output_sha256": hashlib.sha256(raw).hexdigest()})
        self.assertEqual(ran["events"]["response_received"]["content_sha256"], ran["version"][1])
        # The request is still the priced 1024x1536 call; the model is told the frame, and the asset keeps the sent text.
        self.assertEqual(ran["sent"].aspect_ratio, "4:5")
        self.assertIn(canvas_fit.note(canvas_fit.plan("openai", "4:5")), ran["sent"].prompt)
        self.assertEqual(ran["asset_prompt"], ran["sent"].prompt)

    def test_another_provider_keeps_its_output_and_prompt_unchanged(self):
        raw = striped()
        ran = self.run_one("mock", raw)
        self.assertEqual(ran["stored"], raw)
        self.assertNotIn("canvas_fit", ran["events"]["dispatch_started"])
        self.assertNotIn("canvas_fit", ran["events"]["response_received"])
        self.assertNotIn("1024x1280", ran["sent"].prompt)
