"""AI QC and result explanations write in the round's strategy language (J-43, #254).

Real HTTP, generation, durable jobs and costs; only the vision transport is faked and Mock runs are explicit.
"""
import copy
import hashlib
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.adapters.persistence.database import platform_session, tenant_session
from app.adapters.persistence.models import WorkspaceRecord, new_id
from app.entitlements.cost_models import SpendReservationRecord
from app.marketing import ai_qc, ai_qc_text, explanation, workflow
from app.marketing.ai_qc_models import MarketingAIQCRecord
from app.marketing.ai_qc_provider import system_prompt
from app.marketing.models import PlanningCallRecord
from app.marketing.service import digest
from app.marketing.workflow_models import MarketingRoundRecord
from app.platform.jobs import enqueue_in_session
from app.platform.operations import create_operation
from tests.test_marketing_ai_qc import MarketingAIQCFixtures
import tests.test_marketing_followup as followup_tests

CJK = re.compile(r"[　-〿㐀-鿿＀-￯]")
# Field names, check ids and plan keys the rules name; both languages must name the same ones.
IDENTIFIER = re.compile(r"[a-z]+(?:[_.][a-z]+)+(?:=[a-z]+)?|`[a-z_]+`")


def identifiers(sentence):
    return sorted(set(IDENTIFIER.findall(sentence)))


def cjk_free(value):
    return not CJK.search(json.dumps(value, ensure_ascii=False))


class TestTemplates(unittest.TestCase):
    """The en-US and zh-CN templates correspond sentence by sentence; pre-J-43 prompts stay byte-identical."""

    def test_every_sentence_exists_in_both_languages_and_names_the_same_rules(self):
        for name, sentences in ai_qc_text.SENTENCES.items():
            self.assertEqual(set(sentences), set(ai_qc_text.LANGUAGES), name)
            self.assertFalse(CJK.search(sentences["en-US"]), name)
            self.assertTrue(CJK.search(sentences["zh-CN"]), name)
            self.assertEqual(identifiers(sentences["en-US"]), identifiers(sentences["zh-CN"]), name)
        for table in (ai_qc_text.MOCK, ai_qc_text.GATE_FIX, ai_qc_text.FILE_FINDING, ai_qc_text.CONFIRM_NOTE,
                      ai_qc_text.CLAIM_SEPARATOR, explanation.PROMPT, explanation.TEXT):
            self.assertEqual(set(table), set(ai_qc_text.LANGUAGES))
            self.assertTrue(cjk_free(table["en-US"]))
        self.assertEqual(set(explanation.TEXT["en-US"]), set(explanation.TEXT["zh-CN"]))
        for key, value in explanation.TEXT["en-US"].items():
            if isinstance(value, dict):
                self.assertEqual(set(value), set(explanation.TEXT["zh-CN"][key]), key)
            else:
                self.assertEqual(re.findall(r"{\w+}", value), re.findall(r"{\w+}", explanation.TEXT["zh-CN"][key]), key)

    def test_single_and_paired_prompts_follow_the_frozen_language_with_the_same_structure(self):
        checks = {k: "x" for k in ("language_correct", "brand_style", "text_readable", "product_fidelity", "facts_supported")}
        for kind, required in (("single", checks), ("paired", {"belongs_to_intended_combination": "x", "comparison_faithful": "y"})):
            english = system_prompt({"kind": kind, "strategy_language": "en-US", "required_checks": required})
            chinese = system_prompt({"kind": kind, "strategy_language": "zh-CN", "required_checks": required})
            self.assertTrue(cjk_free(english), kind)
            self.assertIn("in English", english)
            self.assertIn("用简体中文书写", chinese)
            schema = english[english.index("Schema:\n"):]
            self.assertEqual(schema, chinese[chinese.index("Schema:\n"):], "the same output schema in both languages")
            self.assertEqual(identifiers(english.split("Schema:")[0]), identifiers(chinese.split("Schema:")[0]), kind)
        single = system_prompt({"kind": "single", "strategy_language": "en-US", "required_checks": {"product_fidelity": "x"}})
        self.assertNotIn("language_correct", single.split("Schema:")[0], "rules still follow the required checks")

    def test_calls_frozen_before_j43_rebuild_their_exact_prompt(self):
        # SHA-256 of each prompt on main 5563faff, before J-43.
        golden = {
            "combined_plain": ({"required_checks": {"product_fidelity": "x", "facts_supported": "y"}},
                "b30749b93f2d72ab0f04517e1bdb969d58027eac6f88e792463f8729600cb85a"),
            "combined_lang_brand": ({"required_checks": {"language_correct": "a", "brand_style": "b", "product_fidelity": "c"}},
                "1244c85305c6737ea536cd01d5e01c0ba71cb499014aca32a11a1c68d1e86454"),
            "single_plain": ({"kind": "single", "required_checks": {"product_fidelity": "x", "facts_supported": "y"}},
                "dacc4e969f41afa221c768825860bac8827fd429c2cb1c31e73d8af21b43e049"),
            "single_lang_brand": ({"kind": "single", "required_checks": {"language_correct": "a", "brand_style": "b", "text_readable": "t"}},
                "e42bf45c6a03f80e9a3778e3abfd996c6d39be4b72c473f3c55a122f41d47950"),
            "paired": ({"kind": "paired", "required_checks": {"belongs_to_intended_combination": "a", "comparison_faithful": "b"}},
                "6ecd19d1df9dfc238312fb33def4d7573bd4b54d805a5d498843e31ea36311f4"),
        }
        for name, (inputs, sha) in golden.items():
            self.assertEqual(hashlib.sha256(system_prompt(inputs).encode()).hexdigest(), sha, name)

    def explanation_inputs(self, language=None, **result):
        base = {"evidence_status": "user_supplied_delivery", "qualified_asset_version_ids": ["v1"], "budget_exhausted": False,
            "round_limit_reached": False, "next_action": "match_expressions_for_candidates", "metric": "ctr", "threshold": "0.012",
            "relative_status": "separated_under_observed_conditions", "big_round_spend": "12.5", "big_round_budget": "100",
            "conditions": {"a": "b"}, "limitations": ["l"], "confirmed_combinations": [], "results": [
                {"id": "asset:v1", "label": "Ad one", "business_status": "passes", "estimate": "0.0153", "interval_low": "0.011", "interval_high": None},
                {"id": "asset:v2", "label": "Ad two", "business_status": "does_not_pass", "estimate": "0.004", "interval_low": "0.001", "interval_high": "0.009"},
                {"id": "group:g1", "label": "G1", "business_status": "passes", "estimate": "0.02", "interval_low": "0.01", "interval_high": "0.03"},
                {"id": "group:g2", "label": "G2", "business_status": "insufficient", "estimate": None, "interval_low": None, "interval_high": None},
                {"id": "group:g3", "label": "G3", "business_status": "uncertain", "estimate": "0", "interval_low": "0", "interval_high": "0"}]}
        plan = {"question": "Q?", "groups": [{"id": "g1", "name": "G1", "style": "warm"}, {"id": "g2", "name": "G2", "style": "cool"}]}
        plan.update(result.pop("plan", {}))
        deviations = result.pop("deviations", {})
        row = SimpleNamespace(id="r1", snapshot_json={"plan": plan, "currency": "USD"})
        evaluation = SimpleNamespace(id="e1", result_json={**base, **result}, input_hash="h", input_json=deviations)
        return explanation.frozen_inputs(row, evaluation, *([language] if language else []))

    SCENARIOS = {
        "plain": ({}, "3ea2836188cc7072479918d312963b760e213bd0ed3067f4c659d605b0b344be"),
        "not_supplied_simulated": ({"evidence_status": "simulated", "budget_exhausted": True},
            "51736f3662be9782b111f6ff8414d6009edc7d1b569259e15a9478d0f75d1cb1"),
        "none_supplied": ({"evidence_status": "not_supplied", "relative_status": "not_compared", "next_action": "x"},
            "c9b0e369f23c9254a7280925f2c0e670b5e5e48d07a89e64f3b01e786b705f92"),
        "tested": ({"plan": {"style_variation": "tested"}, "relative_status": "insufficient", "round_limit_reached": True},
            "01a36fbdcd9ff052a9432f269f008d31b69b5e696ceddee63e35d029a937ca4c"),
        "held_dev": ({"plan": {"style_variation": "held"}, "relative_status": "no_clear_difference", "deviations": {"style_deviations": [1, 2]}},
            "6a75be6cffacec6cf740ec0eda78b1f4fc7650c03aaf10a371760f6179352667"),
        "held_ok": ({"plan": {"style_variation": "held"}}, "df441e91c058143303edcf5bd41fbdfab1abe08738ed62996818a382bd9b40f8"),
    }

    def test_explanation_catalogue_in_zh_cn_is_the_pre_j43_text_and_en_us_offers_the_same_evidence(self):
        self.assertEqual(hashlib.sha256(explanation.system_prompt().encode()).hexdigest(),
            "7c1a9661b6dd970066cb78b3def3215504b02c32551cc85970e40d2d9e64bcf3")
        self.assertTrue(cjk_free(explanation.system_prompt({"strategy_language": "en-US"})))
        for name, (scenario, sha) in self.SCENARIOS.items():
            chinese = self.explanation_inputs(None, **copy.deepcopy(scenario))
            self.assertEqual(chinese.pop("strategy_language"), "zh-CN")
            self.assertEqual(hashlib.sha256(json.dumps(chinese, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), sha, name)
            english = self.explanation_inputs("en-US", **copy.deepcopy(scenario))
            self.assertEqual(english["strategy_language"], "en-US")
            self.assertTrue(cjk_free(english["catalogue"]), name)
            self.assertEqual([(i["id"], i["object_id"], sorted(i["interpretations"])) for i in english["catalogue"]],
                [(i["id"], i["object_id"], sorted(i["interpretations"])) for i in chinese["catalogue"]], name)


class TestAIOutputLanguage(MarketingAIQCFixtures):
    FLAGS = "?include_board=true&include_qc_kinds=true&include_languages=true"
    STRATEGY = "en-US"

    def plan_call(self, project, kind="understanding", mode="mock", quote_hash="", key=None):
        if kind != "plan":
            return super().plan_call(project, kind, mode, quote_hash, key)
        response = self.client.post(f"/api/v1/marketing/projects/{project['id']}/planning", headers=self.headers(project["revision"], key),
            json={"kind": kind, "execution_mode": mode, "quote_hash": quote_hash, "ad_language": "en-US", "strategy_language": self.STRATEGY})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def round(self, row, flags=FLAGS):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}{flags}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def quote(self, row, kind=None, language=None):
        params = {**({"kind": kind} if kind else {}), **({"strategy_language": language} if language else {})}
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}/ai-qc/quote", params=params or None)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def run_kind(self, row, kind, complete=None, language=None):
        quote = self.quote(row, kind, language)
        self.assertTrue(quote["available"], quote)
        row = self.command(row, "ai-qc", {"quote_hash": quote["quote_hash"], "kind": kind,
            **({"strategy_language": language} if language else {})}, 202)
        return row, self.execute_checks(row, complete)

    def passing(self, provider, model, key, inputs, images):
        return self.report(inputs)

    def calls(self, row):
        with tenant_session(self.context) as session:
            return {c.id: c for c in session.scalars(select(MarketingAIQCRecord).where(
                MarketingAIQCRecord.workspace_id == self.context.workspace_id, MarketingAIQCRecord.round_id == row["id"])).all()}

    def drop_plan_language(self, row):
        """A round frozen from a plan written before strategy_language existed."""
        with tenant_session(self.context) as session:
            record = session.get(MarketingRoundRecord, row["id"])
            plan = {k: v for k, v in record.snapshot_json["plan"].items() if k != "strategy_language"}
            record.snapshot_json = {**record.snapshot_json, "plan": plan}

    def legacy_single_call(self, row, asset_id):
        """A single-image call exactly as written before J-43: no strategy_language, policy v1, Chinese prompt."""
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, row["id"])
            inputs = ai_qc.frozen_input(session, self.context, frozen, asset_id, kind="single")
            self.assertNotIn("strategy_language", inputs)
            call_id = new_id("maqc")
            op = create_operation(session, command_type="marketing.ai_qc")
            job = enqueue_in_session(session, context=self.context, job_type="marketing.ai_qc", payload={"call_id": call_id},
                idempotency_key=call_id, resource_type="marketing_ai_qc", resource_id=call_id, safe_to_retry_after_crash=True, max_attempts=1)
            policy = ai_qc.LEGACY_POLICIES["single"]
            session.add(MarketingAIQCRecord(id=call_id, workspace_id=self.context.workspace_id, round_id=frozen.id, asset_version_id=asset_id,
                kind="single", operation_id=op.id, job_id=job.id, retry_of_id=None, sequence=1, state="queued", execution_mode="mock",
                provider="mock", model="marketing-qc-example-v1", maximum_cost="0", policy_version=policy,
                input_hash=digest(inputs), request_hash=digest([digest(inputs), None, "mock", "marketing-qc-example-v1", policy]),
                input_json=inputs))
        return call_id

    def assert_english(self, call):
        self.assertTrue(cjk_free(call.input_json["system_prompt"]))
        self.assertTrue(cjk_free(ai_qc.effective_report(call)), "summary, evidence, rationale, fixes and gates")

    def test_mock_single_and_paired_write_in_the_plan_strategy_language(self):
        project, row = self.setup_round()
        self.assertEqual(self.round(row)["snapshot"]["plan"]["strategy_language"], self.STRATEGY)
        row, _ = self.run_kind(row, "single")
        row, _ = self.run_kind(self.round(row), "paired")
        calls = self.calls(row)
        self.assertEqual({c.kind for c in calls.values()}, {"single", "paired"})
        for call in calls.values():
            self.assertEqual((call.input_json["strategy_language"], call.policy_version), (self.STRATEGY, ai_qc.POLICIES[call.kind]))
            self.assertEqual(call.result_json["source"], "controlled_mock")
            self.assertEqual(call.result_json["summary"], ai_qc_text.MOCK[self.STRATEGY]["summary"])
            if self.STRATEGY == "en-US":
                self.assert_english(call)
            else:
                self.assertTrue(CJK.search(call.result_json["findings"][0]["rationale"]))
                self.assertIn("用简体中文书写", call.input_json["system_prompt"])
        data = self.round(row)
        for asset in data["assets"]:
            self.assertEqual((asset["ai_qc"]["language"], asset["paired_ai_qc"]["language"]), (self.STRATEGY, self.STRATEGY))
            file_valid = asset["ai_qc"]["result"]["findings"][0]
            self.assertEqual(file_valid["check_id"], "file_valid")
            self.assertEqual(file_valid["rationale"], ai_qc_text.FILE_FINDING[self.STRATEGY]["rationale"])
        legacy = self.round(row, "?include_qc_kinds=true")
        self.assertTrue(all("language" not in a["ai_qc"] and "language" not in a["paired_ai_qc"] for a in legacy["assets"]),
            "strict readers from before J-43 keep the old wire shape")

    def test_input_hash_includes_language_so_another_language_is_a_new_paid_call(self):
        project, row = self.setup_round(paid=True)
        self.drop_plan_language(row)
        target = row["assets"][0]["asset_version_id"]
        with tenant_session(self.context) as session:
            frozen = workflow.get_round(session, self.context, row["id"])
            hashes = {language: digest(ai_qc.frozen_input(session, self.context, frozen, target, kind="single", language=language))
                for language in (None, "en-US", "zh-CN")}
        self.assertEqual(len(set(hashes.values())), 3, "legacy, English and Chinese inputs are three different calls")
        english, chinese = self.quote(row, "single", "en-US"), self.quote(row, "single", "zh-CN")
        self.assertNotEqual(english["quote_hash"], chinese["quote_hash"])
        self.assertEqual(self.quote(row, "single")["quote_hash"], chinese["quote_hash"], "no plan or request language: the old Chinese prompt")
        crossed = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/ai-qc", headers=self.headers(row["revision"]),
            json={"quote_hash": english["quote_hash"], "kind": "single", "strategy_language": "zh-CN"})
        self.assertEqual((crossed.status_code, crossed.json()["code"]), (409, "qc_quote_changed"))

        rejected = AsyncMock(side_effect=ValueError("qc_output_incomplete_or_refused"))
        row, first = self.run_kind(row, "single", rejected, language="en-US")
        calls = self.calls(row)
        self.assertEqual({calls[i].state for i in first}, {"failed"})
        self.assertEqual({calls[i].input_json["strategy_language"] for i in first}, {"en-US"})
        retry = self.quote(self.round(row), "single", "zh-CN")
        self.assertEqual(retry["quantity"], len(first), "retrying in another language is a new quoted call")
        row, second = self.run_kind(self.round(row), "single", AsyncMock(side_effect=self.passing), language="zh-CN")
        calls = self.calls(row)
        with tenant_session(self.context) as session:
            reservations = {i: session.scalar(select(SpendReservationRecord).where(SpendReservationRecord.job_id == calls[i].job_id))
                for i in first + second}
            self.assertTrue(all(r is not None for r in reservations.values()), "each language attempt reserved its own maximum")
        for old, new in zip(sorted(first, key=lambda i: calls[i].asset_version_id), sorted(second, key=lambda i: calls[i].asset_version_id)):
            self.assertEqual((calls[new].retry_of_id, calls[new].input_json["strategy_language"]), (old, "zh-CN"))
            self.assertNotEqual(calls[new].input_hash, calls[old].input_hash)
            self.assertNotEqual(calls[new].request_hash, calls[old].request_hash)
        self.assertEqual(self.quote(self.round(row), "single", "en-US")["unavailable_reason"], "nothing_to_check",
            "a current Chinese answer is shown as recorded, never billed again to translate it")

    def test_pre_j43_chinese_record_keeps_its_hash_stays_current_and_is_labelled(self):
        project, row = self.setup_round()
        target = row["assets"][0]["asset_version_id"]
        call_id = self.legacy_single_call(row, target)
        self.execute_checks(row)
        with tenant_session(self.context) as session:
            stored = session.get(MarketingAIQCRecord, call_id)
            before = (stored.input_hash, copy.deepcopy(stored.input_json), copy.deepcopy(stored.result_json))
        self.assertTrue(CJK.search(before[2]["summary"]))
        data = self.round(row)
        asset = next(a for a in data["assets"] if a["asset_version_id"] == target)
        self.assertEqual((asset["ai_qc"]["id"], asset["ai_qc"]["current"], asset["ai_qc"]["language"]), (call_id, True, "zh-CN"))
        self.assertEqual(asset["ai_qc"]["policy_version"], "marketing-vision-qc-single-v1")
        self.assertEqual(asset["ai_qc"]["result"]["summary"], before[2]["summary"], "shown as recorded, not translated")
        self.assertEqual(asset["ai_qc"]["result"]["findings"][0]["rationale"], ai_qc_text.FILE_FINDING["en-US"]["rationale"],
            "the file finding it was shown with before J-43")
        self.assertNotIn(target, self.quote(row, "single", "en-US")["asset_version_ids"], "a current answer is not billed again")
        row, _ = self.run_kind(row, "single", language="en-US")
        self.assertEqual({c.input_json.get("strategy_language") for c in self.calls(row).values()}, {None, "en-US"})
        with tenant_session(self.context) as session:
            stored = session.get(MarketingAIQCRecord, call_id)
            self.assertEqual((stored.input_hash, stored.input_json, stored.result_json), before)

    def test_other_and_inactive_workspaces_cannot_quote_start_or_read_the_language(self):
        project, row = self.setup_round()
        row, _ = self.run_kind(row, "single")
        quote = self.quote(row, "paired", "zh-CN")
        original = self.context
        self.context = self._make_context()
        path = f"/api/v1/marketing/rounds/{row['id']}"
        self.assertEqual(self.client.get(path + "/ai-qc/quote", params={"kind": "single", "strategy_language": "zh-CN"}).status_code, 404)
        self.assertEqual(self.client.post(path + "/ai-qc", headers=self.headers(row["revision"]),
            json={"quote_hash": quote["quote_hash"], "kind": "paired", "strategy_language": "zh-CN"}).status_code, 404)
        self.assertEqual(self.client.get(path + self.FLAGS).status_code, 404)
        self.context = original
        invalid = self.client.get(path + "/ai-qc/quote", params={"kind": "single", "strategy_language": "fr-FR"})
        self.assertEqual(invalid.status_code, 422, "only the two strategy languages exist")
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "suspended"
        denied = self.client.post(path + "/ai-qc", headers=self.headers(row["revision"]),
            json={"quote_hash": quote["quote_hash"], "kind": "paired", "strategy_language": "zh-CN"})
        self.assertEqual(denied.status_code, 403, denied.text)
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "active"
        self.assertEqual({c.kind for c in self.calls(row).values()}, {"single"}, "no call was admitted in another tenant or while inactive")


class TestAIOutputLanguageChinese(TestAIOutputLanguage):
    """The same flows on a plan drafted from the zh-CN interface."""
    STRATEGY = "zh-CN"

    test_input_hash_includes_language_so_another_language_is_a_new_paid_call = None
    test_pre_j43_chinese_record_keeps_its_hash_stays_current_and_is_labelled = None
    test_other_and_inactive_workspaces_cannot_quote_start_or_read_the_language = None


class TestExplanationLanguage(MarketingAIQCFixtures):
    FLAGS = "?include_languages=true"

    def evaluated(self, without_plan_language=False):
        """`without_plan_language`: the round was frozen from a plan written before strategy_language existed."""
        if not without_plan_language:
            return followup_tests.TestMarketingFollowup.evaluated(self)
        project, row = self.bound()
        with tenant_session(self.context) as session:
            record = session.get(MarketingRoundRecord, row["id"])
            record.snapshot_json = {**record.snapshot_json, "plan": {k: v for k, v in record.snapshot_json["plan"].items()
                if k != "strategy_language"}}
        row = self.client.get(f"/api/v1/marketing/rounds/{row['id']}").json()["data"]
        row = self.command(row, "metrics/preview", self.csv(row), 201)
        row = self.command(row, "metrics/confirm", {"batch_id": row["batches"][-1]["id"]})
        return project, self.command(row, "evaluate")

    def explanation_calls(self, row):
        with tenant_session(self.context) as session:
            return [c for c in session.scalars(select(PlanningCallRecord).where(PlanningCallRecord.workspace_id == self.context.workspace_id,
                PlanningCallRecord.kind == "explanation")).all() if c.input_json["round_id"] == row["id"]]

    def read_round(self, row, flags=FLAGS):
        response = self.client.get(f"/api/v1/marketing/rounds/{row['id']}{flags}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def test_mock_explanation_is_written_in_the_plan_strategy_language(self):
        _, row = self.evaluated()
        self.assertEqual(self.read_round(row)["snapshot"]["plan"]["strategy_language"], "en-US")
        row = self.command(row, "explanation", {"execution_mode": "mock", "strategy_language": "zh-CN"}, 202)
        call = self.explanation_calls(row)[0]
        self.assertEqual(call.input_json["strategy_language"], "en-US", "the plan's language wins over the interface")
        self.execute({"id": call.id})
        data = self.read_round(row)
        self.assertEqual((data["explanation"]["state"], data["explanation"]["language"]), ("succeeded", "en-US"))
        paragraphs = data["explanation"]["data"]["paragraphs"]
        self.assertTrue(paragraphs and cjk_free([{k: p[k] for k in ("observation", "explanation")} for p in paragraphs
            if not p["evidence_id"].startswith("result_")]))
        self.assertTrue(all(cjk_free(p["explanation"]) for p in paragraphs))
        self.assertNotIn("language", self.read_round(row, "")["explanation"], "strict readers from before J-43 keep the old wire shape")

    def test_pre_j43_explanation_answers_zh_cn_and_another_language_is_a_new_call(self):
        _, row = self.evaluated(without_plan_language=True)
        row = self.command(row, "explanation", {"execution_mode": "mock", "strategy_language": "zh-CN"}, 202)
        call = self.explanation_calls(row)[0]
        with tenant_session(self.context) as session:
            stored = session.get(PlanningCallRecord, call.id)
            stored.input_json = {k: v for k, v in stored.input_json.items() if k != "strategy_language"}  # as written before J-43
        self.execute({"id": call.id})
        data = self.read_round(row)
        self.assertEqual((data["explanation"]["id"], data["explanation"]["language"], data["explanation"]["current"]), (call.id, "zh-CN", True))
        before = copy.deepcopy(data["explanation"]["data"])
        self.assertTrue(CJK.search(before["paragraphs"][0]["explanation"]))
        again = self.command(data, "explanation", {"execution_mode": "mock", "strategy_language": "zh-CN"}, 202)
        self.assertEqual(again["explanation"]["id"], call.id, "the old Chinese call still answers a zh-CN request")
        english = self.command(again, "explanation", {"execution_mode": "mock", "strategy_language": "en-US"}, 202)
        self.assertNotEqual(english["explanation"]["id"], call.id, "another language is a new call, not a translation")
        calls = {c.id: c for c in self.explanation_calls(row)}
        self.assertEqual(calls[english["explanation"]["id"]].input_json["strategy_language"], "en-US")
        self.assertNotIn("strategy_language", calls[call.id].input_json)
        self.assertEqual(calls[call.id].output_json, before, "the old explanation is never rewritten")

    def test_other_and_inactive_workspaces_cannot_request_an_explanation_in_any_language(self):
        _, row = self.evaluated()
        original = self.context
        self.context = self._make_context()
        foreign = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/explanation", headers=self.headers(row["revision"]),
            json={"execution_mode": "mock", "strategy_language": "en-US"})
        self.assertEqual(foreign.status_code, 404, foreign.text)
        self.context = original
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "suspended"
        denied = self.client.post(f"/api/v1/marketing/rounds/{row['id']}/explanation", headers=self.headers(row["revision"]),
            json={"execution_mode": "mock", "strategy_language": "en-US"})
        self.assertEqual(denied.status_code, 403, denied.text)
        with platform_session() as session:
            session.get(WorkspaceRecord, original.workspace_id).lifecycle = "active"
        self.assertEqual(self.explanation_calls(row), [])


if __name__ == "__main__":
    unittest.main()
