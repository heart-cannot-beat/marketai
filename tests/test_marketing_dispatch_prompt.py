"""#289: product facts reach the image model only as claim evidence, after every rendering constraint.

Maintainer decision (option B): the frozen product content is sent verbatim (no translation, no paid call)
in its own closing section, whose English header says it is not ad copy and must not be rendered. These tests
read the complete prompt the provider receives, not only the draft's frozen `selectors.prompt`.
"""
import asyncio
import json
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import AssetRecord, AssetVersionRecord, GenerationRunRecord, JobRecord
from app.generation.image_tools import instruction
from app.generation.models import GenerationRecipeSnapshotRecord, ProviderAttemptEventRecord, RunItemAssetVersionBindingRecord, RunItemRecord
from app.generation.paid_execution import dispatch_prompt, execute_run_item
from app.generation.selection import product_input
from app.generation.snapshot import _canonical_hash
from app.marketing.languages import CJK
from app.providers.adapter import ProviderCallResult
from tests.support.library_fixtures import png_bytes
from tests.test_marketing_feedback_language import CORRECTIONS, FeedbackLanguageFixtures, render_lines
from tests.test_supplier_image_tools import picture, spec

EDIT = "Reference image 1 is the original creative to edit."


def facts_header(language):
    return (f"Product facts (claim evidence only, not ad copy): never render their words, labels or language "
            f"as image text, except the quoted ad copy in {language} above.")


class DispatchPromptFixtures(FeedbackLanguageFixtures):
    def dispatched(self, draft_id, output=None):
        """Preview, submit and run one draft through the Worker; return the prompts the provider received and the frozen recipe."""
        adapter = AsyncMock()
        adapter.execute.return_value = ProviderCallResult(output or png_bytes(colour=(90, 60, 30)), "external-request", False)
        draft = self.client.get(f"/api/v1/generation/drafts/{draft_id}").json()
        preview = self.client.post(f"/api/v1/generation/drafts/{draft_id}/previews", headers=self.headers(draft["revision"]))
        self.assertEqual(preview.status_code, 201, preview.text)
        run = self.client.post("/api/v1/generation/runs", headers=self.headers(draft["revision"]),
            json={"preview_id": preview.json()["id"], "preview_hash": preview.json()["preview_hash"]})
        self.assertEqual(run.status_code, 202, run.text)
        with tenant_session(self.context) as session:
            run_row = session.get(GenerationRunRecord, run.json()["generation_run_id"])
            snapshot = session.get(GenerationRecipeSnapshotRecord, run_row.recipe_snapshot_id)
            items = [(item.id, session.scalar(select(JobRecord.id).where(JobRecord.workspace_id == self.context.workspace_id,
                JobRecord.resource_id == item.id))) for item in session.scalars(select(RunItemRecord).where(
                RunItemRecord.workspace_id == self.context.workspace_id, RunItemRecord.generation_run_id == run_row.id)).all()]
            self.recipe_hash = snapshot.recipe_hash
            frozen = snapshot.canonical_json["selection"]
        for item_id, job_id in items:
            asyncio.run(execute_run_item(self.context, job_id, item_id, self.blobs, adapter=adapter))
        self.item_ids = [item_id for item_id, _ in items]
        return [call.args[0].prompt for call in adapter.execute.await_args_list], frozen

    def assert_facts_last(self, prompt, frozen, language):
        """Return the text before the facts section after checking the section is the verbatim, last, labelled paragraph."""
        header = facts_header(language)
        self.assertEqual(prompt.count("Product facts ("), 1, prompt)
        start = prompt.index("\n\n" + header + "\n")
        before, facts = prompt[:start], prompt[start + len(header) + 3:]
        # Sent as frozen, untranslated: the fixture product is Chinese.
        self.assertEqual(facts, json.dumps(frozen["product"]["content"], ensure_ascii=False))
        self.assertTrue(CJK.search(facts), facts)
        for line in render_lines(frozen["marketing_intent"]["combination"], language):
            self.assertIn(line, before)
        self.assertIn("Only use claims supported by the Product facts at the end of this prompt.", before)
        self.assertTrue(before.endswith("\nCreative variation 1 of 1."), before)
        return before


class TestEnglishAdDispatchPrompt(DispatchPromptFixtures):
    def test_first_generation_sends_chinese_facts_only_in_the_closing_unrendered_section(self):
        project = self.confirmed()
        pending = self.generate(project, count=0)
        prompts, frozen = self.dispatched(pending[0])
        self.assertEqual(len(prompts), 1)
        before = self.assert_facts_last(prompts[0], frozen, "en-US")
        self.assertIsNone(CJK.search(before), before)
        self.assertTrue(before.startswith(frozen["prompt"] + "\n"), before)

    def test_quality_feedback_revision_sends_facts_after_corrections_and_the_edit_instruction(self):
        _, row = self.checked_round()
        row = self.prepare(row, row["assets"][0], note="Make the headline larger and keep it inside the safe area.")
        prompts, frozen = self.dispatched(row["feedback_drafts"][-1]["draft_id"])
        self.assertTrue(frozen["source_image"])
        before = self.assert_facts_last(prompts[0], frozen, "en-US")
        self.assertIsNone(CJK.search(before), before)
        self.assertTrue(before.startswith("Revise the original creative (reference image 1)"), before)
        self.assertLess(before.index(CORRECTIONS), before.index(EDIT))

    def test_image_tool_edit_sends_facts_after_the_tool_instruction(self):
        _, row = self.setup_round()
        row = self.command(row, "feedback-drafts", {"asset_version_id": row["assets"][0]["asset_version_id"],
            "provider": "mock", "model": "mock-v1", "image_tool": spec("upscale")}, 201)
        prompts, frozen = self.dispatched(row["feedback_drafts"][-1]["draft_id"], output=picture())
        before = self.assert_facts_last(prompts[0], frozen, "en-US")
        self.assertIsNone(CJK.search(before), before)
        # The tool instruction is also part of the frozen correction note; the dispatch adds it again after the edit line.
        self.assertLess(before.index(EDIT), before.rindex(instruction(frozen["image_tool"])))

    def test_frozen_product_and_recipe_are_unchanged_and_the_evidence_names_the_layout(self):
        project = self.confirmed()
        pending = self.generate(project, count=0)
        prompts, frozen = self.dispatched(pending[0])
        with tenant_session(self.context) as session:
            current = product_input(session, self.context, frozen["product"]["id"])
            snapshot = session.scalar(select(GenerationRecipeSnapshotRecord).where(
                GenerationRecipeSnapshotRecord.workspace_id == self.context.workspace_id,
                GenerationRecipeSnapshotRecord.recipe_hash == self.recipe_hash))
            event = session.scalar(select(ProviderAttemptEventRecord).where(
                ProviderAttemptEventRecord.workspace_id == self.context.workspace_id,
                ProviderAttemptEventRecord.event_type == "dispatch_started",
                ProviderAttemptEventRecord.provider_call_attempt_id == session.scalar(
                    select(RunItemAssetVersionBindingRecord.producing_provider_attempt_id).where(
                        RunItemAssetVersionBindingRecord.run_item_id == self.item_ids[0]))))
            binding = session.scalar(select(RunItemAssetVersionBindingRecord).where(
                RunItemAssetVersionBindingRecord.run_item_id == self.item_ids[0]))
            asset = session.get(AssetRecord, session.get(AssetVersionRecord, binding.asset_version_id).asset_id)
            # The recipe freezes the product content as stored; the dispatch layout is not part of it.
            self.assertEqual(frozen["product"], current)
            self.assertEqual(snapshot.recipe_hash, _canonical_hash(snapshot.canonical_json))
            self.assertEqual(snapshot.canonical_json["selection"], frozen)
            self.assertNotIn("Product facts (", frozen["prompt"])
            self.assertEqual(event.payload_json["prompt_layout"], "product-facts-last-v1")
            self.assertEqual(asset.prompt, prompts[0])


class TestChineseAdDispatchPrompt(DispatchPromptFixtures):
    AD_LANGUAGE = "zh-CN"

    def test_chinese_ad_renders_its_own_copy_and_keeps_facts_last(self):
        project = self.confirmed()
        pending = self.generate(project, count=0)
        prompts, frozen = self.dispatched(pending[0])
        before = self.assert_facts_last(prompts[0], frozen, "zh-CN")
        combo = frozen["marketing_intent"]["combination"]
        # The only Chinese before the facts is the quoted zh-CN ad copy the brief asks the model to render.
        quoted = [json.dumps(combo[key], ensure_ascii=False) for key in ("headline", "body_copy")]
        self.assertTrue(any(CJK.search(text) for text in quoted), quoted)
        self.assertIsNone(CJK.search(before.replace(quoted[0], "").replace(quoted[1], "")), before)


class TestPlainDraftDispatchPrompt(DispatchPromptFixtures):
    def test_a_draft_without_an_ad_language_still_gets_facts_last(self):
        # A plain generation draft has no confirmed ad language, so the header cannot rule out the prompt's own language.
        frozen = {"product": {"content": {"name": "Alpha 充电器"}}, "prompt": "A charger on a desk.",
                  "format": {"variants": 2}}
        self.assertEqual(dispatch_prompt(frozen, 1), "A charger on a desk.\nCreative variation 2 of 2.\n\n"
            "Product facts (claim evidence only, not ad copy): never render their words or labels as image text.\n"
            '{"name": "Alpha 充电器"}')
