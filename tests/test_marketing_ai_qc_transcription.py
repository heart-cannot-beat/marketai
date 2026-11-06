"""Single-image QC transcribes the rendered copy and the server compares it (#294); a refused output keeps its evidence (#295).

Real HTTP, generation, durable jobs and costs; only the vision transport is faked and Mock runs are explicit.
"""
import copy
import hashlib
import json
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.adapters.persistence.database import tenant_session
from app.marketing import ai_qc, ai_qc_text, workflow
from app.marketing.ai_qc_contracts import AICheckReport, AICheckTranscribedReport, report_model
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.ai_qc_provider import output_schema, simulated_completion, system_prompt
from app.marketing.ai_qc_worker import EXCERPT_CHARS
from app.marketing.service import digest
from app.adapters.persistence.models import new_id
from app.platform.jobs import enqueue_in_session
from app.platform.operations import create_operation
from tests.test_marketing_ai_qc import MarketingAIQCFixtures

V2 = "marketing-vision-qc-single-v2"
# The J-50 image: the confirmed copy said Volt and interlock, the image rendered Vols and inerlock.
HEADLINE = "Acme Volt Plus: One Adapter, Every Charger"
BODY = "Charge your EV at Halcyon Superchargers with the Acme Volt Plus, built with a safety interlock."
MISSPELT = "Charge your EV at Halcyon Superchargers with the Acme Vols Plus, built with a safety inerlock."


def misspelt(text):
    """The confirmed copy with its second character dropped, in any script."""
    return text[:1] + text[2:]


class TranscriptionFixtures(MarketingAIQCFixtures):
    FLAGS = "?include_qc_kinds=true&include_qc_evidence=true"

    def round_data(self, row, flags=FLAGS):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}{flags}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def single_input(self, row, asset_id=None, **kwargs):
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, row["id"])
            return ai_qc.frozen_input(session, self.context, frozen, asset_id or row["assets"][0]["asset_version_id"],
                kind="single", **kwargs)

    def calls(self, row):
        with tenant_session(self.context) as session:
            return {c.id: c for c in session.scalars(select(MarketingAIQCRecord).where(
                MarketingAIQCRecord.workspace_id == self.context.workspace_id, MarketingAIQCRecord.round_id == row["id"])).all()}

    def j50_input(self, row):
        inputs = self.single_input(row, language="en-US")
        inputs["combination"].update(headline=HEADLINE, body_copy=BODY)
        return inputs

    @staticmethod
    def copy_check(report):
        return next(f for f in report["findings"] if f["check_id"] == "copy_consistent")


class TestRenderedCopyComparison(TranscriptionFixtures):
    def test_misspelt_rendered_copy_fails_copy_consistent_with_the_word_diff(self):
        project, row = self.setup_round()
        inputs = self.j50_input(row)
        self.assertEqual(inputs["policy_version"], "marketing-vision-qc-single-v3")
        report = self.report(inputs)
        report["summary"] = "Overall quality check passed; all text is in English with no spelling errors."
        report["rendered_text"]["body_copy"] = MISSPELT
        normalized = ai_qc.normalize_report(report, inputs)
        finding = self.copy_check(normalized)
        self.assertEqual((finding["status"], finding["model_status"], finding["gate_reason"]), ("fail", "pass", "rendered_text_mismatch"))
        self.assertEqual(finding["rendered_text_diff"], [{"field": "body_copy", "expected": BODY, "rendered": MISSPELT, "changes": [
            {"expected": "Volt", "rendered": "Vols"}, {"expected": "interlock", "rendered": "inerlock"}]}])
        self.assertEqual(finding["suggested_fix"],
            ai_qc_text.GATE_FIX["en-US"]["rendered_text_mismatch"] + "“Vols” → “Volt”, “inerlock” → “interlock”")
        self.assertEqual(normalized["quality_suggestion"], "needs_changes", "the summary's 'passed' is not the verdict")
        self.assertEqual(normalized["rendered_text"], {"headline": HEADLINE, "body_copy": MISSPELT})

    def test_case_whitespace_and_punctuation_never_count_but_words_and_percent_do(self):
        project, row = self.setup_round()
        inputs = self.j50_input(row)
        report = self.report(inputs)
        report["rendered_text"] = {"headline": "ACME VOLT PLUS — one adapter,\nevery charger!",
            "body_copy": "Charge your EV at Halcyon Superchargers with the Acme Volt Plus built with a safety interlock"}
        finding = self.copy_check(ai_qc.normalize_report(report, inputs))
        self.assertEqual((finding["status"], finding.get("gate_reason")), ("pass", None))
        self.assertNotIn("rendered_text_diff", finding)
        inputs["combination"]["headline"] = "Save 20% today"
        report["rendered_text"]["headline"] = "Save 20 today"
        finding = self.copy_check(ai_qc.normalize_report(report, inputs))
        self.assertEqual(finding["gate_reason"], "rendered_text_mismatch")
        self.assertEqual(finding["rendered_text_diff"][0]["changes"], [{"expected": "%", "rendered": ""}])
        inputs["combination"]["headline"] = "了解产品的实际用途"
        report["rendered_text"]["headline"] = "了解产品的实际用逾"
        finding = self.copy_check(ai_qc.normalize_report(report, inputs))
        self.assertEqual(finding["rendered_text_diff"][0]["changes"], [{"expected": "途", "rendered": "逾"}])

    def test_missing_transcription_is_unknown_and_never_erases_an_observed_failure(self):
        project, row = self.setup_round()
        inputs = self.j50_input(row)
        report = self.report(inputs)
        report["rendered_text"]["headline"] = " "
        normalized = ai_qc.normalize_report(report, inputs)
        finding = self.copy_check(normalized)
        self.assertEqual((finding["status"], finding["gate_reason"]), ("unknown", "rendered_text_missing"))
        self.assertEqual(finding["rendered_text_diff"], [{"field": "headline", "expected": HEADLINE, "rendered": "", "changes": []}])
        self.assertEqual(normalized["quality_suggestion"], "needs_confirmation")
        self.copy_check(report).update(status="fail", rationale="The image contradicts the body copy.")
        finding = self.copy_check(ai_qc.normalize_report(report, inputs))
        self.assertEqual(finding["status"], "fail", "a missing transcription never erases the model's own failure")
        report["rendered_text"]["body_copy"] = MISSPELT
        self.assertEqual(self.copy_check(ai_qc.normalize_report(report, inputs))["gate_reason"], "rendered_text_mismatch")
        with self.assertRaises(ValueError):
            ai_qc.normalize_report({k: v for k, v in report.items() if k != "rendered_text"}, inputs)

    def test_effective_report_reapplies_the_comparison_from_the_stored_transcription(self):
        from types import SimpleNamespace
        project, row = self.setup_round()
        inputs = self.j50_input(row)
        report = self.report(inputs)
        report["rendered_text"]["body_copy"] = MISSPELT
        stored = {**ai_qc.normalize_report(report, inputs), "source": "ai_visual_suggestion"}
        original = copy.deepcopy(stored)
        effective = ai_qc.effective_report(SimpleNamespace(result_json=stored, input_json=inputs, kind="single"))
        self.assertEqual(self.copy_check(effective)["gate_reason"], "rendered_text_mismatch")
        self.assertEqual(effective["rendered_text"]["body_copy"], MISSPELT)
        self.assertEqual(stored, original, "reading never rewrites the stored model output")

    def test_v3_prompt_and_schema_ask_for_rendered_text_and_v2_paired_and_mock_stay_as_they_were(self):
        project, row = self.setup_round()
        inputs = self.single_input(row, language="en-US")
        self.assertIs(report_model(inputs), AICheckTranscribedReport)
        schema = inputs["output_schema"]
        self.assertEqual(list(schema["properties"])[0], "rendered_text", "the model transcribes before it judges")
        self.assertIn("rendered_text", schema["required"])
        self.assertEqual(schema["$defs"]["RenderedText"]["required"], ["headline", "body_copy"])
        self.assertFalse(schema["$defs"]["RenderedText"]["additionalProperties"])
        self.assertIn(ai_qc_text.SENTENCES["transcribe"]["en-US"], inputs["system_prompt"])
        # SHA-256 of the v2 prompts and schemas on main 4f8fe969, before #294.
        golden = {
            "single_v2_en": ({"kind": "single", "strategy_language": "en-US", "policy_version": V2,
                "required_checks": {"copy_consistent": "a", "language_correct": "b", "product_fidelity": "c"}},
                "baf1b3ef6622ac20a8fab2bb1a9cd2f70a5de6f3c50b8ed85118dd22adfd1b9b",
                "91b461984261ff8ca931d8183cd1ac7410616e69e747707b8f8a954fdbfffb02"),
            "single_v2_zh": ({"kind": "single", "strategy_language": "zh-CN", "policy_version": V2,
                "required_checks": {"copy_consistent": "a", "brand_style": "b", "text_readable": "t"}},
                "f4f9e687803795dfb051f7ed308c2b8c27f30e0cd9c32a2452136b5c5813e60d",
                "4ad66d709418f8c30842443620d258082235a4c38ba3f0f4f492df6518dedfc3"),
            "paired_v2_en": ({"kind": "paired", "strategy_language": "en-US", "policy_version": ai_qc.POLICIES["paired"],
                "required_checks": {"belongs_to_intended_combination": "a", "comparison_faithful": "b"}},
                "4f8cf3183d38c75033a264e99958b9839e7a21bb1587ab495f1bd1163ae40f52",
                "60a7653d26dcd3613fa7bc46104cd94a0811a14897979cf3f2d861cd036e8ffa"),
        }
        for name, (frozen, prompt_sha, schema_sha) in golden.items():
            self.assertIs(report_model(frozen), AICheckReport, name)
            self.assertEqual(hashlib.sha256(system_prompt(frozen).encode()).hexdigest(), prompt_sha, name)
            self.assertEqual(hashlib.sha256(json.dumps(output_schema(frozen), sort_keys=True).encode()).hexdigest(), schema_sha, name)
        self.assertNotIn("rendered_text", simulated_completion({**inputs, "policy_version": V2}, []))
        mock = ai_qc.normalize_report(simulated_completion(inputs, []), inputs)
        self.assertEqual((self.copy_check(mock)["status"], self.copy_check(mock)["gate_reason"]), ("unknown", "rendered_text_missing"),
            "a placeholder shows no copy, so Mock never passes the copy check")

    def test_paired_output_keeps_its_schema_and_refuses_a_transcription(self):
        project, row = self.setup_round()
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, row["id"])
            inputs = ai_qc.frozen_input(session, self.context, frozen, row["assets"][0]["asset_version_id"], kind="paired", language="en-US")
        self.assertEqual(inputs["policy_version"], "marketing-vision-qc-paired-v2")
        self.assertNotIn("rendered_text", inputs["output_schema"]["properties"])
        self.assertNotIn("rendered_text", inputs["system_prompt"].split("Schema:")[0])
        report = self.report(inputs)
        normalized = ai_qc.normalize_report(report, inputs)
        self.assertNotIn("rendered_text", normalized)
        self.assertTrue(all("rendered_text_diff" not in f for f in normalized["findings"]))
        with self.assertRaises(ValueError):
            ai_qc.normalize_report({**report, "rendered_text": {"headline": "", "body_copy": ""}}, inputs)


class TestTranscriptionFlow(TranscriptionFixtures):
    def misspelling(self, provider, model, key, inputs, images):
        report = self.report(inputs)
        if "rendered_text" in report:
            report["rendered_text"]["body_copy"] = misspelt(inputs["combination"]["body_copy"])
        return report

    def test_mismatch_reaches_the_reader_blocks_bulk_approval_and_a_human_can_still_override(self):
        project, row = self.setup_round(paid=True)
        ids = self.run_both(row, AsyncMock(side_effect=self.misspelling))
        data = self.round_data(row)
        asset = data["assets"][0]
        single, paired = asset["ai_qc"], asset["paired_ai_qc"]
        finding = self.copy_check(single["result"])
        self.assertEqual((finding["status"], finding["gate_reason"]), ("fail", "rendered_text_mismatch"))
        self.assertEqual(finding["rendered_text_diff"][0]["field"], "body_copy")
        self.assertEqual(finding["rendered_text_diff"][0]["rendered"], misspelt(asset["body_copy"]))
        self.assertTrue(finding["rendered_text_diff"][0]["changes"])
        self.assertEqual(single["result"]["rendered_text"]["headline"], asset["headline"])
        self.assertEqual(single["result"]["quality_suggestion"], "needs_changes")
        self.assertIsNone(single["error_detail"])
        board = self.round_data(row, "?include_board=true&include_qc_kinds=true")["board"]
        node = next(n for a in board["tree"]["angles"] for x in a["expressions"] for c in x["combinations"]
            for n in c["executions"] if n["asset_version_id"] == asset["asset_version_id"])
        self.assertEqual(node["ai_qc"]["quality_suggestion"], "needs_changes")

        # Readers that predate #294/#295 see the shape they validate; the verdict is the same.
        legacy = self.round_data(row, "?include_qc_kinds=true")["assets"][0]
        self.assertNotIn("rendered_text", legacy["ai_qc"]["result"])
        self.assertNotIn("error_detail", legacy["ai_qc"])
        self.assertTrue(all("rendered_text_diff" not in f for f in legacy["ai_qc"]["result"]["findings"]))
        self.assertEqual(self.copy_check(legacy["ai_qc"]["result"])["status"], "fail")

        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/confirm", headers=self.headers(data["revision"]),
            json={"ai_qc_ids": ids, "resolved_checks": {"copy_consistent": "pass"}, "note": "Looks fine"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "qc_failure_requires_review")

        body = {"asset_version_id": asset["asset_version_id"], "quality": "approved",
            "checks": {key: "pass" for key in asset["required_checks"]}, "note": "Reviewed the exact image",
            "ai_qc_id": single["id"], "paired_ai_qc_id": paired["id"], "compared_asset_version_ids": paired["compared_asset_version_ids"]}
        response = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/reviews", headers=self.headers(data["revision"]), json=body)
        self.assertEqual(response.json()["code"], "qc_override_note_required")
        self.command(data, "reviews", {**body, "override_note": "I read the image myself: the copy is rendered correctly."}, 201)
        reviewed = self.round_data(row)["assets"][0]
        self.assertTrue(reviewed["ready"])
        self.assertEqual(reviewed["quality"]["overrides"]["copy_consistent"], {"ai": "fail", "human": "pass"})
        stored = self.copy_check({"findings": reviewed["quality"]["effective_findings"]})
        self.assertEqual(stored["gate_reason"], "rendered_text_mismatch")
        self.assertTrue(stored["rendered_text_diff"], "the review freezes the diff it overrode")
        legacy = self.round_data(row, "?include_qc_kinds=true")["assets"][0]
        self.assertTrue(all("rendered_text_diff" not in f for f in legacy["quality"]["effective_findings"]))

    def test_a_v2_answer_stays_current_untranscribed_and_is_not_billed_again(self):
        project, row = self.setup_round()
        target = row["assets"][0]["asset_version_id"]
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, row["id"])
            inputs = ai_qc.frozen_input(session, self.context, frozen, target, kind="single", language="en-US", policy=V2)
            self.assertNotIn("rendered_text", inputs["output_schema"]["properties"])
            call_id = new_id("maqc")
            op = create_operation(session, command_type="marketing.ai_qc")
            job = enqueue_in_session(session, context=self.context, job_type="marketing.ai_qc", payload={"call_id": call_id},
                idempotency_key=call_id, resource_type="marketing_ai_qc", resource_id=call_id, safe_to_retry_after_crash=True, max_attempts=1)
            session.add(MarketingAIQCRecord(id=call_id, workspace_id=self.context.workspace_id, round_id=frozen.id, asset_version_id=target,
                kind="single", operation_id=op.id, job_id=job.id, retry_of_id=None, sequence=1, state="queued", execution_mode="mock",
                provider="mock", model="marketing-qc-example-v1", maximum_cost="0", policy_version=V2,
                input_hash=digest(inputs), request_hash=digest([digest(inputs), None, "mock", "marketing-qc-example-v1", V2]),
                input_json=inputs))
        self.execute_checks(row)
        call = next(a for a in self.round_data(row)["assets"] if a["asset_version_id"] == target)["ai_qc"]
        self.assertEqual((call["id"], call["state"], call["current"], call["policy_version"]), (call_id, "succeeded", True, V2))
        self.assertNotIn("rendered_text", call["result"])
        self.assertEqual(self.copy_check(call["result"])["gate_reason"], "low_confidence", "an old report is read as it was")
        quote = self.quote(row, "single")
        self.assertNotIn(target, quote["asset_version_ids"], "a current v2 answer is never billed again to transcribe")
        self.assertEqual(len(quote["asset_version_ids"]), len(row["assets"]) - 1)


class TestRefusedOutputEvidence(TranscriptionFixtures):
    def failed_calls(self, row):
        """The newest attempt per image, every one of them refused."""
        newest = {}
        for call in sorted(self.calls(row).values(), key=lambda c: c.sequence):
            newest[call.asset_version_id] = call
        self.assertTrue(newest and all(c.state == "failed" for c in newest.values()))
        return list(newest.values())

    def test_invalid_output_keeps_reason_field_bounded_excerpt_and_hash_but_never_the_key(self):
        project, row = self.setup_round(paid=True)
        returned, keys = {}, set()

        async def too_long(provider, model, key, inputs, images):
            report = self.report(inputs)
            report["findings"][0]["visual_evidence"] = "x" * 5000
            returned[inputs["asset_version_id"]] = json.dumps(report, ensure_ascii=False)
            keys.add(key.decode())
            return returned[inputs["asset_version_id"]]
        row, ids = self.run_kind(row, "single", AsyncMock(side_effect=too_long))
        data = self.round_data(row)
        for call in self.failed_calls(row):
            text, detail = returned[call.asset_version_id], call.error_detail
            self.assertEqual((call.error_code, call.result_json), ("qc_output_invalid", None))
            self.assertEqual((detail["reason"], detail["field"], detail["problem"]),
                ("qc_output_schema_invalid", "findings.0.visual_evidence", "string_too_long"))
            self.assertGreater(len(text), EXCERPT_CHARS)
            self.assertEqual(detail["excerpt"], text[:EXCERPT_CHARS])
            self.assertEqual((detail["sha256"], detail["length"]), (hashlib.sha256(text.encode()).hexdigest(), len(text)))
            self.assertFalse(any(key in json.dumps(detail) for key in keys))
            asset = next(a for a in data["assets"] if a["asset_version_id"] == call.asset_version_id)
            self.assertEqual((asset["ai_qc"]["state"], asset["ai_qc"]["error_detail"]), ("failed", detail))
            self.assertFalse(asset["ready"])
        self.assertNotIn("error_detail", self.round_data(row, "?include_qc_kinds=true")["assets"][0]["ai_qc"])
        self.assertTrue(self.quote(row, "single")["available"], "a retry is a new, explicit paid intent")

    def test_each_refusal_names_its_reason(self):
        cases = {
            "qc_output_not_json": (lambda inputs: "I could not inspect the image.", None),
            "qc_check_coverage_invalid": (lambda inputs: {**self.report(inputs), "findings": self.report(inputs)["findings"][1:]}, "findings"),
            "qc_fact_reference_invalid": (lambda inputs: {**self.report(inputs), "findings": [
                {**f, "fact_ids": ["not-a-fact"]} if n == 0 else f for n, f in enumerate(self.report(inputs)["findings"])]}, "findings.0.fact_ids"),
        }
        project, row = self.setup_round(paid=True)
        for reason, (output, field) in cases.items():
            with self.subTest(reason):
                # Each round of refusals is retried as a new, explicit paid intent.
                row, _ = self.run_kind(self.round_data(row), "single", AsyncMock(side_effect=lambda p, m, k, i, images: output(i)))
                for call in self.failed_calls(row):
                    detail = call.error_detail
                    self.assertEqual((detail["reason"], detail["field"]), (reason, field))
                    self.assertTrue(detail["excerpt"] and len(detail["sha256"]) == 64)

    def test_provider_refusal_and_another_workspace_expose_no_output_evidence(self):
        project, row = self.setup_round(paid=True)
        row, _ = self.run_kind(row, "single", AsyncMock(side_effect=ValueError("qc_output_incomplete_or_refused")))
        call = self.failed_calls(row)[0]
        self.assertEqual((call.error_code, call.error_detail), ("qc_provider_rejected", None), "nothing returned, nothing to keep")
        original = self.context
        self.context = self._make_context()
        self.assertEqual(self.client.get(f"/api/v1/marketing/rounds/{row['id']}{self.FLAGS}").status_code, 404)
        self.context = original
        self.assertEqual(self.round_data(row)["assets"][0]["ai_qc"]["error_code"], "qc_provider_rejected")
