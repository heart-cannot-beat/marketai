"""Words an ad image shows besides its copy (#265).

The image model may write things the ad never said: a range on a dashboard, a price on a bill, a badge. From
``ideas-brief-v2`` the image prompt forbids any text besides the headline and body; from
``marketing-ideas-qc-single-v2`` the ad's check transcribes every other text it sees (``other_text``) and the
server checks it against the facts the ad was drafted on (``ai_qc.extra_text_gate``). Drafts, corrections and
checks made under v1 keep their exact prompts.
"""
import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.marketing import ai_qc, ai_qc_text, ideas, ideas_revisions
from app.marketing.ai_qc_contracts import AICheckFullTranscribedReport, AICheckTranscribedReport, report_model
from app.marketing.ai_qc_provider import output_schema, simulated_completion
from app.marketing.service import ONLY_COPY_TEXT
from app.marketing.workflow_models import MarketingFeedbackDraftRecord

from tests.test_marketing_first_batch import check_report
from tests.test_marketing_ideas_checks import IdeasChecksFixtures
from tests.test_marketing_ideas_revisions import RevisionFixtures

V1 = "marketing-ideas-qc-single-v1"
V2 = ai_qc.IDEAS_POLICY
CHECKS = {"copy_consistent": "a", "facts_supported": "b", "product_fidelity": "c", "language_correct": "d"}
HEADLINE = "Power your drive with 7 kW charging"
BODY = "Schedule charging for off-peak hours in the Acme Charge app."


def facts():
    return [
        {"id": "f1", "statement": "It charges at up to 7 kW, about 25 miles of range per hour.",
         "source_quote": "It charges at up to 7 kW, which adds about 25 miles of range per hour on a typical EV.",
         "confirmation": "unconfirmed"},
        {"id": "f2", "statement": "Price: $449, with a 3-year warranty.",
         "source_quote": "Price: $449, with a 3-year warranty.", "confirmation": "unconfirmed"},
    ]


def inputs(policy=V2, language="en-US"):
    """The parts of a frozen ideas check the server's gates read."""
    return {"policy_version": policy, "kind": "single", "strategy_language": language, "required_checks": CHECKS,
            "combination": {"headline": HEADLINE, "body_copy": BODY},
            "understanding": {"product_name": "Acme Charge Home", "facts": facts()},
            "evidence_fact_ids": ["f1", "f2"], "product_reference_available": True}


def answer(frozen, other_text=(), **finding):
    """The model's report: every check passes; the copy is rendered exactly; ``other_text`` besides it."""
    report = {"rendered_text": {"headline": HEADLINE, "body_copy": BODY},
              "summary": "The ad shows its claim.", "findings": [
                  {"check_id": key, "status": "pass", "confidence": "high", "visual_evidence": "The charger is shown.",
                   "rationale": "Matches the ad.", "suggested_fix": "", "fact_ids": ["f1"] if key == "facts_supported" else []}
                  for key in frozen["required_checks"]]}
    if frozen["policy_version"] == V2:
        report["rendered_text"]["other_text"] = list(other_text)
    next(f for f in report["findings"] if f["check_id"] == "facts_supported").update(finding)
    return report


def facts_check(normalized):
    return next(f for f in normalized["findings"] if f["check_id"] == "facts_supported")


class ExtraTextGateTests(unittest.TestCase):
    def test_a_number_or_price_the_facts_do_not_state_fails_and_every_extra_text_is_listed(self):
        normalized = ai_qc.normalize_report(answer(inputs(), ["Range 250 mi", "$68.25", "Acme Charge Home"]), inputs())
        finding = facts_check(normalized)
        self.assertEqual((finding["status"], finding["gate_reason"]), ("fail", "extra_text_unsupported"))
        self.assertEqual(finding["extra_text"], ["Range 250 mi", "$68.25"], "the product's own name is not extra")
        self.assertEqual(finding["unsupported_claims"], ["$68.25"])
        self.assertIn("$68.25", finding["suggested_fix"])
        self.assertEqual(normalized["quality_suggestion"], "needs_changes")

    def test_words_the_facts_cannot_settle_literally_need_a_person_to_look(self):
        normalized = ai_qc.normalize_report(answer(inputs(), ["Meets all standards", "Up to 7 kW"]), inputs())
        finding = facts_check(normalized)
        self.assertEqual((finding["status"], finding["gate_reason"]), ("unknown", "extra_text"))
        self.assertEqual(finding["extra_text"], ["Meets all standards", "Up to 7 kW"])
        self.assertEqual(finding.get("unsupported_claims", []), [])
        self.assertIn("“Meets all standards”", finding["suggested_fix"])
        self.assertEqual(normalized["quality_suggestion"], "needs_confirmation")

    def test_pieces_of_the_copy_and_the_product_name_are_not_extra(self):
        other = ["7 kW", "Acme Charge", "ACME CHARGE HOME", "Schedule charging", "  ", "Acme Charge app."]
        finding = facts_check(ai_qc.normalize_report(answer(inputs(), other), inputs()))
        self.assertEqual((finding["status"], finding.get("gate_reason"), finding.get("extra_text")), ("pass", None, None))

    def test_the_same_text_twice_is_listed_once(self):
        finding = facts_check(ai_qc.normalize_report(answer(inputs(), ["Meets all standards", "meets all standards!"]),
                                                     inputs()))
        self.assertEqual(finding["extra_text"], ["Meets all standards"])

    def test_a_check_the_model_failed_keeps_its_own_reason_and_still_lists_the_text(self):
        failed = answer(inputs(), ["Meets all standards"], status="fail", rationale="The image claims a certification.")
        finding = facts_check(ai_qc.normalize_report(failed, inputs()))
        self.assertEqual((finding["status"], finding.get("gate_reason")), ("fail", None))
        self.assertEqual(finding["extra_text"], ["Meets all standards"])

    def test_the_fix_is_written_in_the_check_language(self):
        finding = facts_check(ai_qc.normalize_report(answer(inputs(language="zh-CN"), ["$68.25"]), inputs(language="zh-CN")))
        self.assertTrue(finding["suggested_fix"].startswith(ai_qc_text.GATE_FIX["zh-CN"]["extra_text_unsupported"]))

    def test_reading_again_applies_the_gate_to_the_stored_output_without_rewriting_it(self):
        frozen = inputs()
        stored = {**ai_qc.normalize_report(answer(frozen, ["Range 250 mi"]), frozen), "source": "ai_visual_suggestion"}
        original = copy.deepcopy(stored)
        effective = ai_qc.effective_report(SimpleNamespace(result_json=stored, input_json=frozen, kind="single"))
        self.assertEqual(facts_check(effective)["extra_text"], ["Range 250 mi"])
        self.assertEqual(effective["rendered_text"]["other_text"], ["Range 250 mi"])
        self.assertEqual(stored, original)

    def test_a_v1_check_has_no_other_text_and_no_new_gate(self):
        frozen = inputs(V1)
        self.assertIs(report_model(frozen), AICheckTranscribedReport)
        finding = facts_check(ai_qc.normalize_report(answer(frozen), frozen))
        self.assertEqual((finding["status"], finding.get("extra_text")), ("pass", None))
        with self.assertRaises(Exception):  # a v1 report never carries other_text
            ai_qc.normalize_report({**answer(frozen), "rendered_text": {"headline": HEADLINE, "body_copy": BODY,
                                                                        "other_text": ["$68.25"]}}, frozen)


class PromptAndSchemaTests(unittest.TestCase):
    def test_v2_asks_for_every_other_text_after_the_copy(self):
        frozen = inputs()
        self.assertIs(report_model(frozen), AICheckFullTranscribedReport)
        schema = output_schema(frozen)
        self.assertEqual(schema["$defs"]["RenderedTextWithOthers"]["required"], ["headline", "body_copy", "other_text"])
        self.assertFalse(schema["$defs"]["RenderedTextWithOthers"]["additionalProperties"])
        for language in ai_qc_text.LANGUAGES:
            prompt = ai_qc_text.ideas_single_prompt(CHECKS, language, schema, all_text=True)
            self.assertIn(ai_qc_text.SENTENCES["transcribe_ideas_all"][language], prompt)
            self.assertNotIn(ai_qc_text.SENTENCES["transcribe_ideas"][language], prompt)

    def test_v1_prompt_and_schema_stay_as_they_were(self):
        # SHA-256 of the v1 ideas check prompt and schema on main e5a189d, before #265.
        schema = output_schema({"kind": "single", "policy_version": V1, "required_checks": CHECKS})
        self.assertEqual(hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                         "045313a72a370f5fd4f02f7bd3d4925b5c4f6cd1d9c5856a65e01f455e862953")
        golden = {("en-US", False): "00ad71a196367b06aa673e880f54da40f0416ff41ce9fc86093d5bddd021f66a",
                  ("en-US", True): "659682f78151f1b2efcf0f7b53fea9f9899e50eab6593872b0ad493d62259c09",
                  ("zh-CN", False): "4dbf17c2a9bbe9d321100a6327e6b439c92ba4c1d037e50c2313273362e1cd71",
                  ("zh-CN", True): "e074415d2f735746d6cc32993de2e2f921e2e835c00865ff05efe0eacece5908"}
        for (language, unverified), digest in golden.items():
            prompt = ai_qc_text.ideas_single_prompt(CHECKS, language, schema, unverified_photos=unverified)
            self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), digest, (language, unverified))

    def test_mock_transcribes_nothing_so_it_never_lists_or_passes_anything(self):
        frozen = inputs()
        completion = simulated_completion(frozen, [])
        self.assertEqual(completion["rendered_text"], {"headline": "", "body_copy": "", "other_text": []})
        self.assertEqual(facts_check(ai_qc.normalize_report(completion, frozen))["status"], "unknown")


class ExtraTextCheckTests(IdeasChecksFixtures):
    def test_an_ads_words_besides_its_copy_reach_the_ad_and_an_unsourced_price_fails(self):
        project = self.made("byok")
        self.start_checks(project, "byok")

        async def bill(provider, model, credential, inputs, images):
            self.assertEqual(inputs["policy_version"], V2)
            self.assertIn(ai_qc_text.SENTENCES["transcribe_ideas_all"][inputs["strategy_language"]], inputs["system_prompt"])
            return check_report(inputs, rendered_text={"other_text": ["Electricity bill $68.25", "Range 250 mi"]})
        self.run_checks(project, AsyncMock(side_effect=bill))
        for ad in self.read_ideas(project)["ads"]:
            finding = next(f for f in ad["check"]["findings"] if f["check_id"] == "facts_supported")
            self.assertEqual((finding["status"], finding["gate_reason"]), ("fail", "extra_text_unsupported"))
            self.assertEqual(finding["extra_text"], ["Electricity bill $68.25", "Range 250 mi"])
            self.assertEqual(finding["unsupported_claims"], ["$68.25"])
            self.assertEqual(ad["check"]["suggestion"], "needs_changes")


class ImagePromptTests(RevisionFixtures):
    def draft_of(self, project, combination_id):
        with tenant_session(self.context) as session:
            from app.marketing.models import MarketingGenerationLinkRecord
            link = session.scalar(select(MarketingGenerationLinkRecord).where(
                MarketingGenerationLinkRecord.project_id == project["id"],
                MarketingGenerationLinkRecord.combination_id == combination_id))
            return self.client.get(f"/api/v1/generation/drafts/{link.draft_id}").json()

    def rewrite(self, draft, selectors):
        response = self.client.patch(f"/api/v1/generation/drafts/{draft['id']}", headers=self.headers(draft["revision"]),
                                     json={"selectors": selectors})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_an_ad_image_is_asked_for_no_words_besides_its_copy(self):
        project = self.checked_ads()
        draft = self.draft_of(project, "c1_1")
        self.assertEqual(draft["selectors"]["prompt_policy"], ideas.PROMPT_POLICY)
        self.assertIn("\n" + ONLY_COPY_TEXT, draft["selectors"]["prompt"])

    def test_a_draft_prepared_before_keeps_its_v1_prompt_but_a_mixed_one_is_refused(self):
        project = self.checked_ads()
        draft = self.draft_of(project, "c1_1")
        v1 = {k: v for k, v in draft["selectors"].items() if k != "prompt_policy"}
        v1["prompt"] = v1["prompt"].replace("\n" + ONLY_COPY_TEXT, "")
        draft = self.rewrite(draft, v1)
        _, response = self.preview(draft["id"])
        self.assertEqual(response.status_code, 201, response.text)
        # A v1 prompt under the v2 policy is a prompt the application did not write for it.
        draft = self.rewrite(self.client.get(f"/api/v1/generation/drafts/{draft['id']}").json(),
                             {**v1, "prompt_policy": ideas.PROMPT_POLICY})
        _, response = self.preview(draft["id"])
        self.assertEqual((response.status_code, response.json()["code"]), (422, "ideas_prompt_changed"))

    def test_a_correction_asks_for_no_words_besides_the_copy_and_one_frozen_under_v1_keeps_its_prompt(self):
        project = self.checked_ads()
        prepared = self.feedback(project, "c2_1")
        draft = self.client.get(f"/api/v1/generation/drafts/{prepared['draft_id']}").json()
        self.assertIn("\n" + ONLY_COPY_TEXT, draft["selectors"]["prompt"])
        with tenant_session(self.context) as session:
            record = session.get(MarketingFeedbackDraftRecord, prepared["id"])
            self.assertEqual(record.feedback_json["prompt_policy"], ideas_revisions.PROMPT_POLICY)
            record.feedback_json = {**record.feedback_json, "prompt_policy": "ideas-feedback-brief-v1"}
        draft = self.rewrite(draft, {**draft["selectors"],
                                     "prompt": draft["selectors"]["prompt"].replace("\n" + ONLY_COPY_TEXT, "")})
        _, response = self.preview(draft["id"])
        self.assertEqual(response.status_code, 201, response.text)
