"""Issue #164 -- the governed LLM plan drafter.

Unit level (no database): the strict schema, the controlled-mock fixture,
the deterministic mapping of an answer into a ``PlanDraft`` (stable factor
keys and value ids, the 「钩子」 factor, count and reference validation), and
the prompt (product facts, the three answers, winners / losers -- empty
included).

End to end (``TestClient`` + the real Worker + the governed ``llm_chat`` path
against a fake OpenAI): a byok plan reserves its drafting call at creation
and settles it after drafting; a schema violation or an unusable answer ends
the plan ``failed`` with a suggested action; a revoked key is
``credential_unavailable`` (``bind_openai_key``) and releases the unused
reservation; a guardrail breach refuses admission; a re-run Job replays the
stored answer into identical ids; revising with an added direction makes no
LLM call; mock mode maps the EV-charger fixture through the same code;
another Workspace can neither read the plan nor spend its call.
"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from dataclasses import asdict
from decimal import Decimal

from sqlalchemy import delete, select

from tests.test_test_plans_api import (
    PROTECTOR,
    PlanFixtures,
    _contains_key,
    fake_openai_llm_client,
    openai_completion,
)

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AdTestPlanDirectionRecord,
    AdTestPlanRecord,
    OperationRecord,
)
from app.entitlements.cost_models import ProviderCallAttemptRecord, SpendReservationRecord
from app.entitlements.credential_admin import CredentialAdmin
from app.generation.planner import PlanDirectionSpace, enumerate_plan_space, plan_all_stages
from app.library.contracts import ROLE_OWNER
from app.plans import drafting, llm_drafter
from app.plans.llm_drafter import (
    TEST_PLAN_DRAFT_SCHEMA,
    LlmPlanDrafter,
    build_request,
    build_user_prompt,
    map_llm_draft,
)
from app.plans.worker import run_plan_drafting
from app.providers import json_schema
from app.providers.llm_mock import TEST_PLAN_DRAFT_EXAMPLE

FIXTURE = TEST_PLAN_DRAFT_EXAMPLE


def _example() -> dict:
    return copy.deepcopy(FIXTURE)


def _facts(**overrides) -> drafting.ProductFacts:
    base = dict(
        product_id="prod_1", entry_id="lib_1", version_id="libv_1",
        name="VoltNest 家用交流充电桩",
        description="壁挂式家用电动汽车交流充电桩",
        selling_points=["比家用插座快 3 倍，一晚充满", "App 预约谷电时段充电"],
        audiences=[{"who": "有固定车位的新能源车主", "why": "想在家过夜充满", "pain": "公共桩排队"}],
        must_match=[{"type": "color", "description": "哑光黑外壳，正面浅灰色面板"}],
        specs={"额定功率": "7 kW"},
    )
    base.update(overrides)
    return drafting.ProductFacts(**base)


def _audience() -> drafting.AudienceChoice:
    card = {"who": "有固定车位的新能源车主", "why": "想在家过夜充满", "pain": "公共桩排队"}
    return drafting.AudienceChoice(text="有固定车位的新能源车主：想在家过夜充满", card_index=0, card=card)


def _normalized(data: dict) -> drafting.NormalizedPlan:
    return drafting.normalize_plan_draft(map_llm_draft(data))


def _single_valued(data: dict | None = None) -> dict:
    """The answer real models gave on 2026-10-12 (Issue #193): every
    direction picks exactly one value of every factor."""
    data = data or _example()
    for direction in data["directions"]:
        for pair in direction["factor_values"]:
            pair["values"] = pair["values"][:1]
    return data


def _six_presentation_values() -> dict:
    """Production failure: six valid values on one presentation factor."""
    data = _example()
    factor = data["factors"][2]
    factor["label"] = "产品呈现"
    factor["values"] += [
        {"label": f"呈现{i}", "positive_fragments": [f"product presentation {i}"], "negative_fragments": []}
        for i in range(3, 7)
    ]
    for index, direction in enumerate(data["directions"]):
        pair = direction["factor_values"][2]
        pair["factor"] = "产品呈现"
        if index >= 2:
            pair["values"] = [factor["values"][index + 1]["label"], factor["values"][index + 2]["label"]]
    return data


def _reference_drift_example() -> dict:
    data = _example()
    factor = data["factors"][2]
    factor["label"] = "产品呈现"
    factor["values"][0]["label"] = "正面展示"
    factor["values"][1]["label"] = "整体侧面展示带品牌字样"
    for direction in data["directions"]:
        pair = direction["factor_values"][2]
        pair["factor"] = "产品呈现"
        pair["values"] = ["正面展示" if value == "出车" else "整体侧面展示带品牌字样" for value in pair["values"]]
    data["directions"][-1]["title"] = "钩子—工艺质感细节吸引"
    data["directions"][-1]["factor_values"][2]["values"] = ["侧面展示带品牌字样"]
    return data


def _space(normalized: drafting.NormalizedPlan) -> list:
    space, _excluded = enumerate_plan_space(
        [PlanDirectionSpace(d["direction_key"], d["factor_values"]) for d in normalized.directions],
        normalized.constraints,
    )
    return space


# ---------------------------------------------------------------------------
# Unit: schema, fixture, mapping, prompt
# ---------------------------------------------------------------------------


class SchemaAndFixtureTests(unittest.TestCase):
    def test_the_schema_is_openai_strict_mode_compatible(self):
        def walk(node, path="$"):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, path)
                    self.assertEqual(sorted(node["required"]), sorted(node["properties"]), path)
                for bound in ("minItems", "maxItems", "minLength", "maxLength", "pattern"):
                    self.assertNotIn(bound, node, path)
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{path}[{index}]")

        walk(TEST_PLAN_DRAFT_SCHEMA)

    def test_the_mock_fixture_satisfies_the_schema(self):
        self.assertEqual(json_schema.validate(_example(), TEST_PLAN_DRAFT_SCHEMA), [])


class MappingTests(unittest.TestCase):
    def test_a_shortened_display_reference_preserves_the_catalog_and_records_the_repair(self):
        data = _reference_drift_example()
        original = copy.deepcopy(data)
        first = map_llm_draft(data)
        second = map_llm_draft(copy.deepcopy(data))
        self.assertEqual(data, original)
        self.assertEqual(first.evidence["resolved_references"], [{
            "factor": "car", "reference": "侧面展示带品牌字样", "value_label": "整体侧面展示带品牌字样",
        }])
        self.assertEqual(asdict(drafting.normalize_plan_draft(first)), asdict(drafting.normalize_plan_draft(second)))

    def test_reference_repair_does_not_guess_negation_color_or_numbers(self):
        for reference in ("侧面展示不带品牌字样", "侧面展示带其他品牌字样", "侧面展示带品牌字样2", "黑色侧面展示带品牌字样"):
            with self.subTest(reference=reference):
                data = _reference_drift_example()
                data["directions"][-1]["factor_values"][2]["values"] = [reference]
                with self.assertRaises(drafting.DraftInvalid) as failed:
                    map_llm_draft(data)
                self.assertEqual(failed.exception.code, "unknown_factor_value")

    def test_a_colliding_display_alias_is_rejected(self):
        data = _reference_drift_example()
        factor = data["factors"][2]
        factor["values"][1]["label"] += "（黑）"
        factor["values"].append({"label": "侧面展示带品牌字样(黑)", "positive_fragments": [], "negative_fragments": []})
        for direction in data["directions"]:
            pair = direction["factor_values"][2]
            pair["values"] = [value + "（黑）" if value == "整体侧面展示带品牌字样" else value for value in pair["values"]]
        data["directions"][-1]["factor_values"][2]["values"] = ["侧面展示带品牌字样（黑）"]
        with self.assertRaises(drafting.DraftInvalid) as failed:
            map_llm_draft(data)
        self.assertEqual(failed.exception.code, "ambiguous_factor_value")

    def test_value_count_uses_the_domain_ceiling_without_truncating_choices(self):
        for count in (2, 4, 6, drafting.MAX_VALUES_PER_FACTOR):
            with self.subTest(count=count):
                data = _example()
                data["factors"][2]["values"] += [
                    {"label": f"呈现{i}", "positive_fragments": ["product detail"], "negative_fragments": []}
                    for i in range(3, count + 1)
                ]
                normalized = _normalized(data)
                self.assertEqual(len(normalized.factors[2]["values"]), count)
                self.assertEqual(asdict(normalized), asdict(_normalized(data)))
        too_few = _example()
        too_few["factors"][2]["values"] = too_few["factors"][2]["values"][:1]
        with self.assertRaises(drafting.DraftInvalid) as failed:
            map_llm_draft(too_few)
        self.assertEqual(failed.exception.code, "factor_values_invalid")

    def test_the_ev_charger_answer_maps_into_a_plan(self):
        draft = map_llm_draft(_example())
        self.assertEqual((draft.drafter, draft.drafter_version), ("llm", "llm-plan-drafter/v4"))
        self.assertEqual([f.key for f in draft.factors], ["people", "scene", "car", "hook"])
        self.assertEqual([f.label for f in draft.factors], ["人物", "场景", "汽车", "钩子"])
        self.assertEqual([v.label for v in draft.factors[1].values], ["工业", "生活", "产品"])
        self.assertTrue(all(v.positive_fragments for f in draft.factors for v in f.values))

        normalized = drafting.normalize_plan_draft(draft)
        self.assertEqual([d["direction_key"] for d in normalized.directions], ["dir-1", "dir-2", "dir-3", "dir-4"])
        first = normalized.directions[0]
        self.assertEqual(first["title"], "回家插上，一晚充满")
        self.assertIn("是否", first["what_it_tests"])
        self.assertEqual(first["copy"]["headline"], "回家插上，早上满电")
        self.assertEqual(
            first["factor_values"],
            {"people": ["people-1"], "scene": ["scene-2"], "car": ["car-1", "car-2"], "hook": ["hook-1"]},
        )
        self.assertEqual(
            normalized.constraints,
            [{"type": "excludes", "factor_a": "scene", "value_a": "scene-3", "factor_b": "people", "value_b": "people-1"}],
        )
        hook = next(f for f in normalized.factors if f["key"] == "hook")
        self.assertEqual([v["id"] for v in hook["values"]], ["hook-1", "hook-2", "hook-3"])
        self.assertEqual(hook["values"][1]["label"], "谷电省钱")

        space, _excluded = enumerate_plan_space(
            [PlanDirectionSpace(d["direction_key"], d["factor_values"]) for d in normalized.directions],
            normalized.constraints,
        )
        self.assertEqual(len(space), 10)
        stages = plan_all_stages(
            [PlanDirectionSpace(d["direction_key"], d["factor_values"]) for d in normalized.directions],
            normalized.constraints,
        )
        self.assertEqual({scale: len(stage.selected) for scale, stage in stages.items()},
                         {"sample": 4, "small": 10, "full": 10})

    def test_mapping_is_deterministic_so_a_replayed_answer_yields_the_same_ids(self):
        first = _normalized(_example())
        second = _normalized(json.loads(json.dumps(_example(), ensure_ascii=False)))
        self.assertEqual(asdict(first), asdict(second))

    def test_factor_keys_are_derived_stably_from_hints_and_labels(self):
        data = _example()
        data["factors"][0]["key"] = "人物"  # no ASCII: hashed from the label
        data["factors"][1]["key"] = "Scene Type"  # slugged
        data["factors"][2]["key"] = "scene_type"  # clashes with factor 2 -> hashed suffix
        data["factors"][3]["key"] = "marketing angle"  # the hook is always `hook`
        keys = [f.key for f in map_llm_draft(data).factors]
        self.assertEqual(keys[1], "scene_type")
        self.assertEqual(keys[3], "hook")
        self.assertRegex(keys[0], r"^f_[0-9a-f]{6}$")
        self.assertRegex(keys[2], r"^scene_type_[0-9a-f]{6}$")
        self.assertEqual(keys, [f.key for f in map_llm_draft(copy.deepcopy(data)).factors])
        for key in keys:
            self.assertRegex(key, drafting.FACTOR_KEY_PATTERN.pattern)
        reserved = _example()
        reserved["factors"][0]["key"] = "direction"
        self.assertNotEqual(map_llm_draft(reserved).factors[0].key, "direction")

    def test_directions_may_reference_factors_by_key_and_values_case_insensitively(self):
        data = _example()
        data["directions"][0]["factor_values"][0]["factor"] = "people"
        data["factors"][2]["values"][0]["label"] = "Car In"
        for direction in data["directions"]:
            for pair in direction["factor_values"]:
                pair["values"] = ["car in" if value == "出车" else value for value in pair["values"]]
        self.assertEqual(_normalized(data).directions[0]["factor_values"]["car"], ["car-1", "car-2"])

    def test_unusable_answers_are_draft_invalid(self):
        cases = {}
        no_hook = _example()
        no_hook["factors"][3]["is_hook"] = False
        no_hook["factors"][3]["label"] = "卖点"
        cases["hook_factor_missing"] = no_hook
        two_hooks = _example()
        two_hooks["factors"][0]["is_hook"] = True
        cases["hook_factor_ambiguous"] = two_hooks
        few = _example()
        few["directions"] = few["directions"][:2]
        cases["direction_count_invalid"] = few
        one_factor = _example()
        one_factor["factors"] = one_factor["factors"][3:]
        cases["factor_count_invalid"] = one_factor
        too_many_values = _example()
        too_many_values["factors"][1]["values"] += [
            {"label": f"场景{i}", "positive_fragments": ["x"], "negative_fragments": []} for i in range(6)
        ]
        cases["factor_values_invalid"] = too_many_values
        unknown_value = _example()
        unknown_value["directions"][0]["factor_values"][1]["values"] = ["月球"]
        cases["unknown_factor_value"] = unknown_value
        unknown_factor = _example()
        unknown_factor["directions"][0]["factor_values"][1]["factor"] = "天气"
        cases["unknown_factor"] = unknown_factor
        missing = _example()
        missing["directions"][1]["factor_values"] = missing["directions"][1]["factor_values"][:3]
        cases["direction_factor_missing"] = missing
        duplicate = _example()
        duplicate["factors"][0]["values"][1]["label"] = "有人"
        cases["duplicate_factor_value"] = duplicate
        untested = _example()
        untested["directions"][2]["what_it_tests"] = "  "
        cases["what_it_tests_missing"] = untested
        for code, data in cases.items():
            with self.subTest(code=code):
                self.assertEqual(json_schema.validate(data, TEST_PLAN_DRAFT_SCHEMA), [])
                with self.assertRaises(drafting.DraftInvalid) as raised:
                    map_llm_draft(data)
                self.assertEqual(raised.exception.code, code)

    def test_extra_directions_are_dropped_and_unknown_exclusions_ignored(self):
        data = _example()
        data["directions"] += copy.deepcopy(data["directions"][:3])
        data["exclusions"].append({"factor_a": "天气", "value_a": "雨", "factor_b": "人物", "value_b": "有人"})
        draft = map_llm_draft(data)
        self.assertEqual(len(draft.directions), 5)
        self.assertEqual(draft.evidence["dropped_directions"], 2)
        self.assertEqual(draft.evidence["ignored_exclusions"], 1)
        self.assertEqual(len(draft.constraints), 1)


class WideningTests(unittest.TestCase):
    """Issue #193: every direction compares something; 6-32 combinations."""

    def _assert_each_direction_compares(self, normalized: drafting.NormalizedPlan) -> None:
        for direction in normalized.directions:
            widths = {key: len(ids) for key, ids in direction["factor_values"].items() if key != "hook"}
            self.assertTrue(any(width >= 2 for width in widths.values()), direction["title"])

    def test_the_fixture_already_compares_so_nothing_is_widened(self):
        draft = map_llm_draft(_example())
        self.assertEqual(draft.evidence["auto_widened"], [])
        self.assertEqual(draft.evidence["drafted_combinations"], 10)
        normalized = drafting.normalize_plan_draft(draft)
        self._assert_each_direction_compares(normalized)
        self.assertTrue(6 <= len(_space(normalized)) <= 32)

    def test_an_all_single_valued_answer_is_widened_with_values_other_directions_use(self):
        data = _single_valued()
        self.assertEqual(json_schema.validate(data, TEST_PLAN_DRAFT_SCHEMA), [])
        draft = map_llm_draft(data)
        widened = draft.evidence["auto_widened"]
        self.assertEqual(draft.evidence["drafted_combinations"], 4, "sample == full before widening")
        self.assertEqual([item["direction"] for item in widened], ["dir-1", "dir-2", "dir-3", "dir-4"])
        self.assertTrue(all(item["reason"] == "single_valued" for item in widened))
        self.assertTrue(all(item["source"] == "other_direction" for item in widened))
        self.assertNotIn("hook", {item["factor"] for item in widened}, "the hook is never widened")
        # dir-1 (有人) borrows 无人 from dir-3; dir-4 (产品 + 无人) cannot take 有人
        # (excluded with 产品), so it borrows the scene 生活 instead.
        self.assertEqual([(item["factor"], item["value_label"]) for item in widened],
                         [("people", "无人"), ("people", "无人"), ("people", "有人"), ("scene", "生活")])

        normalized = drafting.normalize_plan_draft(draft)
        self._assert_each_direction_compares(normalized)
        self.assertEqual(normalized.directions[0]["factor_values"],
                         {"people": ["people-1", "people-2"], "scene": ["scene-2"], "car": ["car-1"],
                          "hook": ["hook-1"]})
        self.assertEqual([d["factor_values"]["hook"] for d in normalized.directions],
                         [["hook-1"], ["hook-2"], ["hook-3"], ["hook-1"]])
        self.assertEqual(len(_space(normalized)), 8)
        stages = plan_all_stages(
            [PlanDirectionSpace(d["direction_key"], d["factor_values"]) for d in normalized.directions],
            normalized.constraints,
        )
        self.assertEqual(len(stages["sample"].selected), 4)
        self.assertEqual(len(stages["full"].selected), 8, "the full test now measures more than the sample")

    def test_widening_is_deterministic_so_a_replayed_answer_maps_identically(self):
        first = map_llm_draft(_single_valued())
        second = map_llm_draft(json.loads(json.dumps(_single_valued(), ensure_ascii=False)))
        self.assertEqual(asdict(first), asdict(second))
        self.assertEqual(asdict(drafting.normalize_plan_draft(first)), asdict(drafting.normalize_plan_draft(second)))

    def _with_uses(self, data: dict, uses: dict[str, list[list[str]]]) -> dict:
        for index, direction in enumerate(data["directions"]):
            direction["factor_values"] = [{"factor": f, "values": v[index]} for f, v in uses.items()]
        return data

    def test_widening_never_takes_the_plan_past_32_combinations(self):
        data = _example()
        data["exclusions"] = []
        data["directions"].append({**copy.deepcopy(data["directions"][0]), "title": "第五个方向"})
        uses = {
            "人物": [["有人", "无人"], ["有人", "无人"], ["有人", "无人"], ["有人"], ["无人"]],
            "场景": [["工业", "生活", "产品"], ["工业", "生活", "产品"], ["工业", "生活", "产品"], ["生活"], ["工业"]],
            "汽车": [["出车", "不出车"], ["出车", "不出车"], ["出车"], ["出车"], ["不出车"]],
            "钩子": [["一晚充满"], ["谷电省钱"], ["安全耐用"], ["一晚充满"], ["谷电省钱"]],
        }
        draft = map_llm_draft(self._with_uses(data, uses))
        self.assertEqual(draft.evidence["drafted_combinations"], 12 + 12 + 6 + 1 + 1)
        self.assertEqual(draft.evidence["auto_widened"], [], "either widening would make 33")
        self.assertLessEqual(len(_space(drafting.normalize_plan_draft(draft))), 32)

        # With room for exactly two more, both single-valued directions widen.
        uses["场景"][1] = ["工业", "生活"]
        draft = map_llm_draft(self._with_uses(data, uses))
        self.assertEqual(draft.evidence["drafted_combinations"], 12 + 8 + 6 + 1 + 1)
        self.assertEqual([item["direction"] for item in draft.evidence["auto_widened"]], ["dir-4", "dir-5"])
        self.assertEqual(len(_space(drafting.normalize_plan_draft(draft))), 30)

        # Room for one more combination: dir-4 (two hooks, 2 -> 4) is skipped,
        # dir-5 (1 -> 2) still fits.
        uses["场景"][1] = ["工业", "生活", "产品"]
        uses["汽车"][2] = ["出车", "不出车"]
        uses["场景"][2] = ["工业"]
        uses["钩子"][3] = ["一晚充满", "谷电省钱"]
        draft = map_llm_draft(self._with_uses(data, uses))
        self.assertEqual(draft.evidence["drafted_combinations"], 12 + 12 + 4 + 2 + 1)
        self.assertEqual([item["direction"] for item in draft.evidence["auto_widened"]], ["dir-5"])
        self.assertEqual(len(_space(drafting.normalize_plan_draft(draft))), 32)

    def test_exclusions_that_starve_the_plan_are_topped_up_to_six(self):
        data = _example()
        data["directions"] = data["directions"][:3]
        data["exclusions"] = [{"factor_a": "场景", "value_a": "生活", "factor_b": "人物", "value_b": "无人"}]
        uses = {
            "人物": [["有人", "无人"], ["有人"], ["有人"]],
            "场景": [["生活"], ["工业"], ["产品"]],
            "汽车": [["出车"], ["出车"], ["不出车"]],
            "钩子": [["一晚充满"], ["谷电省钱"], ["安全耐用"]],
        }
        draft = map_llm_draft(self._with_uses(data, uses))
        reasons = [(item["direction"], item["reason"]) for item in draft.evidence["auto_widened"]]
        self.assertEqual(reasons[:2], [("dir-2", "single_valued"), ("dir-3", "single_valued")])
        self.assertIn(("dir-1", "below_minimum"), reasons)
        self.assertGreaterEqual(len(_space(drafting.normalize_plan_draft(draft))), 6)

    def test_the_prompt_asks_for_a_two_valued_factor_and_6_to_32_combinations(self):
        self.assertIn("每个方向必须至少有一个非钩子因素选 2 个取值", llm_drafter.SYSTEM_PROMPT)
        self.assertIn("6~32", llm_drafter.SYSTEM_PROMPT)
        self.assertIn("无人物", llm_drafter.SYSTEM_PROMPT)


class MockDrafterWideningTests(unittest.TestCase):
    def _draft(self, verdicts: list) -> drafting.PlanDraft:
        return asyncio.run(drafting.MockPlanDrafter().draft_plan(
            _facts(), "explore", _audience(), "sample", drafting.WinnersLosers(value_verdicts=verdicts),
        ))

    def test_the_mock_drafter_compares_within_every_direction(self):
        draft = self._draft([])
        self.assertEqual(draft.evidence["auto_widened"], [])
        self.assertTrue(6 <= len(_space(drafting.normalize_plan_draft(draft))) <= 32)

    def test_loser_filtering_that_leaves_single_values_is_widened(self):
        losers = [
            drafting.ValueVerdict("场景", "居家日常", "loser"),
            drafting.ValueVerdict("文案切入", "痛点对比", "loser"),
            drafting.ValueVerdict("人物", "真人使用", "loser"),
        ]
        draft = self._draft(losers)
        self.assertEqual([item["direction"] for item in draft.evidence["auto_widened"]], ["dir-1"])
        normalized = drafting.normalize_plan_draft(draft)
        for direction in normalized.directions:
            self.assertTrue(any(len(ids) >= 2 for ids in direction["factor_values"].values()), direction["title"])
        self.assertTrue(6 <= len(_space(normalized)) <= 32)
        self.assertEqual(asdict(draft), asdict(self._draft(losers)), "deterministic")


class PromptTests(unittest.TestCase):
    def test_empty_winners_and_losers_still_build_a_prompt(self):
        prompt = build_user_prompt(_facts(), "explore", _audience(), "sample", drafting.WinnersLosers())
        self.assertIn("VoltNest 家用交流充电桩", prompt)
        self.assertIn("- 比家用插座快 3 倍，一晚充满", prompt)
        self.assertIn("哑光黑外壳", prompt)
        self.assertIn("痛点：公共桩排队", prompt)
        self.assertIn("探索新方向", prompt)
        self.assertIn("先出样图", prompt)
        self.assertIn("暂无投放结论", prompt)
        request = build_request(_facts(), "explore", _audience(), "sample", drafting.WinnersLosers())
        self.assertEqual((request.semantic, request.schema_name), ("test_plan_draft", "test_plan_draft"))
        self.assertIn("钩子", request.messages[0].text)
        self.assertIn("Meta", request.messages[0].text)

    def test_winners_and_losers_value_labels_reach_the_prompt(self):
        signals = drafting.WinnersLosers(
            winners=[
                drafting.SeedAsset("av_1", verdict="winner", dimension_labels={"钩子": "谷电省钱", "场景": "生活"}),
                drafting.SeedAsset("av_2", verdict="winner", dimension_labels={"钩子": "谷电省钱"}),
            ],
            losers=[drafting.SeedAsset("av_3", verdict="loser", dimension_labels={"场景": "工业"})],
        )
        prompt = build_user_prompt(_facts(), "explore", _audience(), "full", signals)
        winners, losers = prompt.split("落败的取值")
        self.assertIn("- 钩子 = 谷电省钱（2 个素材）", winners)
        self.assertIn("- 场景 = 生活", winners)
        self.assertIn("- 场景 = 工业", losers)
        self.assertIn("exploratory", prompt)
        self.assertNotIn("暂无投放结论", prompt)

    def test_an_optimize_plan_lists_its_baseline_and_falls_back_to_raw_tags(self):
        seeds = drafting.WinnersLosers(winners=[
            drafting.SeedAsset("av_1", verdict="seed", dimension_labels={"创意方向": "回家插上"}),
            drafting.SeedAsset("av_2", verdict="seed", dimension_tags={"lib_x:scene": "scene-2"}),
        ])
        prompt = build_user_prompt(_facts(), "optimize", _audience(), "small", seeds)
        self.assertIn("基线素材（2 个", prompt)
        self.assertIn("- 创意方向 = 回家插上", prompt)
        self.assertIn("- lib_x:scene = scene-2", prompt)
        self.assertIn("暂无投放结论", prompt)  # seeds are a baseline, not learnings

    def test_the_request_is_deterministic_in_its_inputs(self):
        args = (_facts(), "explore", _audience(), "sample", drafting.WinnersLosers())
        self.assertEqual(build_request(*args), build_request(*args))


# ---------------------------------------------------------------------------
# End to end: API -> reservation -> Worker -> governed call -> plan
# ---------------------------------------------------------------------------


class ByokDraftingTests(PlanFixtures):
    def setUp(self) -> None:
        super().setUp()
        self._bind_openai()

    def _answer(self, data: dict | None = None):
        return openai_completion(json.dumps(data if data is not None else _example(), ensure_ascii=False))

    def _reservation(self, reservation_id: str) -> SpendReservationRecord:
        with tenant_session(self.ctx) as session:
            row = session.get(SpendReservationRecord, reservation_id)
            session.expunge(row)
            return row

    def _plan_record(self, plan_id: str) -> AdTestPlanRecord:
        with tenant_session(self.ctx) as session:
            row = session.get(AdTestPlanRecord, plan_id)
            session.expunge(row)
            return row

    def test_shortened_value_reference_reaches_ready_without_another_paid_call(self):
        self._use_fake_openai(self._answer(_reference_drift_example()))
        accepted = self._create({"product_id": self._product()})
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "ready", plan.get("failure"))
        self.assertIn("car-2", plan["directions"][-1]["factor_values"]["car"])
        self.assertEqual(len(plan["factors"][2]["values"]), 2)
        self.assertEqual(len(self.openai_requests), 1)
        with tenant_session(self.ctx) as session:
            session.execute(delete(AdTestPlanDirectionRecord).where(AdTestPlanDirectionRecord.plan_id == accepted["plan_id"]))
            record = session.get(AdTestPlanRecord, accepted["plan_id"])
            record.status = "drafting"
            record.recipe_json = {**record.recipe_json, "factors": [], "constraints": []}
        outcome = asyncio.run(run_plan_drafting(self.ctx, plan_id=accepted["plan_id"], llm_client=self.llm_client))
        self.assertEqual(outcome.kind, "ready")
        replay = self._get(accepted["plan_id"])
        self.assertTrue(replay["drafting"]["call"]["replayed"])
        self.assertEqual(replay["drafting"]["resolved_references"], plan["drafting"]["resolved_references"])
        self.assertEqual(len(self.openai_requests), 1)

    def test_a_genuinely_unknown_reference_retries_inside_the_reserved_call(self):
        broken = _example()
        broken["directions"][0]["factor_values"][1]["values"] = ["月球"]
        self._use_fake_openai(self._answer(broken), self._answer())
        accepted = self._create({"product_id": self._product()})
        reservation_id = self._plan_record(accepted["plan_id"]).evidence_json["admission"]["reservation_id"]
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "ready", plan.get("failure"))
        self.assertEqual(len(self.openai_requests), 2)
        self.assertEqual(plan["drafting"]["call"]["usage"]["input_tokens"], 6000)
        self.assertEqual(plan["drafting"]["call"]["usage"]["output_tokens"], 3000)
        self.assertEqual(self._reservation(reservation_id).state, "settled")
        with tenant_session(self.ctx) as session:
            attempts = session.scalars(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == self.ctx.workspace_id,
                ProviderCallAttemptRecord.job_id == self._plan_record(accepted["plan_id"]).job_id,
            )).all()
            self.assertEqual(len(attempts), 1)

    def test_schema_and_domain_validation_share_one_retry_budget(self):
        bad_schema = _example()
        del bad_schema["exclusions"]
        bad_reference = _example()
        bad_reference["directions"][0]["factor_values"][1]["values"] = ["月球"]
        self._use_fake_openai(self._answer(bad_schema), self._answer(bad_reference))
        accepted = self._create({"product_id": self._product()})
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["failure"]["code"], "draft_invalid")
        self.assertIn("unknown_factor_value", plan["failure"]["detail"])
        self.assertEqual(len(self.openai_requests), 2)

    def test_six_presentation_values_reach_ready_without_another_paid_call(self):
        self._use_fake_openai(self._answer(_six_presentation_values()))
        accepted = self._create({"product_id": self._product(), "scale": "sample"})
        reservation_id = self._plan_record(accepted["plan_id"]).evidence_json["admission"]["reservation_id"]
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "ready", plan.get("failure"))
        self.assertEqual(len(next(f for f in plan["factors"] if f["key"] == "car")["values"]), 6)
        self.assertEqual(plan["directions"][3]["factor_values"]["car"], ["car-5", "car-6"])
        self.assertEqual(plan["stages"]["sample"]["combinations"], 4)
        self.assertEqual(len(self.openai_requests), 1)
        self.assertEqual(self._reservation(reservation_id).state, "settled")
        self.assertEqual(self._audits("test_plan.draft_failed"), [])
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.get(OperationRecord, accepted["operation_id"]).state, "succeeded")

        # A lost worker commit replays the stored answer, with identical value ids.
        with tenant_session(self.ctx) as session:
            session.execute(delete(AdTestPlanDirectionRecord).where(
                AdTestPlanDirectionRecord.plan_id == accepted["plan_id"]))
            record = session.get(AdTestPlanRecord, accepted["plan_id"])
            record.status = "drafting"
            record.recipe_json = {**record.recipe_json, "factors": [], "constraints": []}
        outcome = asyncio.run(run_plan_drafting(
            self.ctx, plan_id=accepted["plan_id"], llm_client=self.llm_client,
        ))
        self.assertEqual(outcome.kind, "ready")
        replay = self._get(accepted["plan_id"])
        self.assertTrue(replay["drafting"]["call"]["replayed"])
        self.assertEqual(replay["factors"], plan["factors"])
        self.assertEqual([d["factor_values"] for d in replay["directions"]],
                         [d["factor_values"] for d in plan["directions"]])
        self.assertEqual(len(self.openai_requests), 1)

        # The larger catalog also publishes and materializes through Generation.
        self._run(accepted["plan_id"], "sample")
        self._drain()
        self.assertEqual(len(self.openai_requests), 1)

    def test_byok_reserves_at_creation_drafts_with_the_llm_and_settles(self):
        self._use_fake_openai(self._answer())
        accepted = self._create({"product_id": self._product()})
        admission = self._plan_record(accepted["plan_id"]).evidence_json["admission"]
        self.assertEqual((admission["drafting_mode"], admission["reserved"]), ("byok", True))
        self.assertEqual(admission["quote"]["provider"], "openai")
        self.assertEqual(admission["quote"]["max_output_tokens"], llm_drafter.MAX_OUTPUT_TOKENS)
        self.assertGreater(Decimal(admission["quote"]["max_amount"]), 0)
        self.assertEqual(self._reservation(admission["reservation_id"]).state, "reserved")

        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "ready", plan.get("failure"))
        self.assertEqual(len(self.openai_requests), 1)
        sent = json.loads(self.openai_requests[0].content)
        self.assertEqual(sent["response_format"]["json_schema"]["name"], "test_plan_draft")
        self.assertTrue(sent["response_format"]["json_schema"]["strict"])
        self.assertIn("VoltNest 充电桩", sent["messages"][-1]["content"])
        self.assertIn("新能源车主", sent["messages"][-1]["content"])
        self.assertEqual(self.openai_requests[0].headers["authorization"], "Bearer sk-ws-own")
        self.assertNotEqual(self._reservation(admission["reservation_id"]).state, "reserved",
                            "settled at reported usage after the call")

        self.assertEqual(plan["drafting"]["drafter"], "llm")
        self.assertEqual(plan["drafting"]["call"]["provider"], "openai")
        self.assertFalse(plan["drafting"]["call"]["replayed"])
        self.assertEqual([d["title"] for d in plan["directions"]], [d["title"] for d in FIXTURE["directions"]])
        self.assertEqual([f["key"] for f in plan["factors"]], ["people", "scene", "car", "hook"])
        self.assertEqual(plan["stages"]["sample"]["combinations"], 4)
        self.assertEqual(plan["stages"]["full"]["combinations"], 10)
        self.assertTrue(all(v["proposed_by"] == "ai" for f in plan["factors"] for v in f["values"]))
        with tenant_session(self.ctx) as session:
            record = session.get(AdTestPlanRecord, accepted["plan_id"])
            forbidden = {"provider", "model", "prompt", "final_prompt", "execution_mode"}
            self.assertFalse(_contains_key(record.recipe_json, forbidden), "recipe stays provider-neutral")
            self.assertEqual(session.get(OperationRecord, accepted["operation_id"]).state, "succeeded")
        drafted = self._audits("test_plan.drafted")
        self.assertEqual(drafted[0].details_json["drafter"], "llm")

        # Revising with an added direction stays deterministic: no LLM call.
        revised = self._revise(plan["plan_id"], {"add": [{"title": "我自己的想法"}]}, revision=plan["revision"])
        self.assertEqual(len(revised["directions"]), 5)
        self.assertEqual(len(self.openai_requests), 1)

    def test_a_schema_violation_fails_the_plan_with_retry(self):
        broken = _example()
        del broken["exclusions"]
        self._use_fake_openai(self._answer(broken), self._answer(broken))
        accepted = self._create({"product_id": self._product()})
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["failure"]["code"], "schema_validation_failed")
        self.assertTrue(plan["failure"]["retryable"])
        self.assertEqual(plan["drafting"]["suggested_action"], "retry")
        self.assertEqual(plan["drafting"]["call"]["error"]["code"], "schema_validation_failed")
        self.assertEqual(len(self.openai_requests), 2, "re-asked once, then failed")
        self.assertEqual(plan["directions"], [])
        with tenant_session(self.ctx) as session:
            self.assertEqual(session.get(OperationRecord, accepted["operation_id"]).state, "failed")
        self.assertEqual(len(self._audits("test_plan.draft_failed")), 1)

    def test_unusable_answers_fail_after_one_bounded_retry_and_settle_once(self):
        no_hook = _example()
        no_hook["factors"][3]["is_hook"] = False
        no_hook["factors"][3]["label"] = "卖点"
        self._use_fake_openai(self._answer(no_hook), self._answer(no_hook))
        accepted = self._create({"product_id": self._product()})
        reservation_id = self._plan_record(accepted["plan_id"]).evidence_json["admission"]["reservation_id"]
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["failure"]["code"], "draft_invalid")
        self.assertIn("hook_factor_missing", plan["failure"]["detail"])
        self.assertEqual(plan["drafting"]["suggested_action"], "retry")
        self.assertTrue(plan["drafting"]["call"]["attempt_id"])
        self.assertEqual(self._reservation(reservation_id).state, "settled", "the answer was paid for")
        self.assertEqual(len(self.openai_requests), 2)
        self.assertEqual(plan["drafting"]["call"]["usage"]["output_tokens"], 3000)
        self.assertEqual(plan["drafting"]["call"]["cost"]["provider_currency"], "USD")
        self.assertGreater(Decimal(plan["drafting"]["call"]["cost"]["provider_amount"]), 0)

    def test_a_revoked_key_is_credential_unavailable_and_releases_the_reservation(self):
        self._use_fake_openai()
        accepted = self._create({"product_id": self._product()})
        reservation_id = self._plan_record(accepted["plan_id"]).evidence_json["admission"]["reservation_id"]
        owner = self.owner_ctx(self.ctx)
        with tenant_session(owner) as session:
            CredentialAdmin(PROTECTOR).revoke(session, owner, credential_key="OPENAI_API_KEY", reason="test")
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["failure"]["code"], "credential_unavailable")
        self.assertFalse(plan["failure"]["retryable"])
        self.assertIn("重新绑定", plan["failure"]["message"])
        self.assertEqual(plan["drafting"]["suggested_action"], "bind_openai_key")
        self.assertEqual(self.openai_requests, [])
        reservation = self._reservation(reservation_id)
        self.assertEqual(reservation.state, "released")
        self.assertEqual(float(reservation.outstanding_budget_amount), 0.0)
        failed = self._audits("test_plan.draft_failed")
        self.assertTrue(failed[0].details_json["reservation_released"])

    def test_a_guardrail_breach_refuses_creation(self):
        from app.generation.spend import ensure_spend_guardrail

        with tenant_session(self.owner_ctx(self.ctx)) as session:
            ensure_spend_guardrail(session, self.ctx).per_operation_limit = Decimal("0.000001")
        refused = self._create({"product_id": self._product()}, expect=409)
        self.assertEqual(refused["code"], "guardrail_exceeded")
        self.assertIn("花费护栏", refused["detail"])
        with tenant_session(self.ctx) as session:
            self.assertIsNone(session.scalars(
                select(AdTestPlanRecord).where(AdTestPlanRecord.workspace_id == self.ctx.workspace_id)
            ).first(), "the whole admission rolled back")

    def test_a_rerun_job_replays_the_paid_answer_into_identical_ids(self):
        self._use_fake_openai(self._answer())
        accepted = self._create({"product_id": self._product()})
        self._drain()
        first = self._get(accepted["plan_id"])
        self.assertEqual(first["status"], "ready")

        # A Job that ran again after the call (e.g. the commit was lost):
        # back to `drafting`, no direction rows, same Job and call key.
        with tenant_session(self.ctx) as session:
            session.execute(delete(AdTestPlanDirectionRecord).where(
                AdTestPlanDirectionRecord.plan_id == accepted["plan_id"]))
            record = session.get(AdTestPlanRecord, accepted["plan_id"])
            record.status = "drafting"
            record.recipe_json = {**record.recipe_json, "factors": [], "constraints": []}
        outcome = asyncio.run(run_plan_drafting(self.ctx, plan_id=accepted["plan_id"], llm_client=self.llm_client))
        self.assertEqual(outcome.kind, "ready")
        self.assertEqual(len(self.openai_requests), 1, "the stored answer replayed; nothing paid twice")
        again = self._get(accepted["plan_id"])
        self.assertTrue(again["drafting"]["call"]["replayed"])
        for key in ("factors", "constraints"):
            self.assertEqual(again[key], first[key])
        strip = ("combination_signatures", "selected_in")
        self.assertEqual(
            [{k: v for k, v in d.items() if k not in strip} for d in again["directions"]],
            [{k: v for k, v in d.items() if k not in strip} for d in first["directions"]],
        )
        self.assertEqual([d["combination_signatures"] for d in again["directions"]],
                         [d["combination_signatures"] for d in first["directions"]])
        with tenant_session(self.ctx) as session:
            attempts = session.scalars(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == self.ctx.workspace_id,
                ProviderCallAttemptRecord.job_id == self._plan_record(accepted["plan_id"]).job_id,
            )).all()
            self.assertEqual(len(attempts), 1)

    def test_a_single_valued_answer_is_widened_once_and_a_rerun_replays_it_unpaid(self):
        self._use_fake_openai(self._answer(_single_valued()))
        accepted = self._create({"product_id": self._product()})
        self._drain()
        first = self._get(accepted["plan_id"])
        self.assertEqual(first["status"], "ready", first.get("failure"))
        widened = first["drafting"]["auto_widened"]
        self.assertEqual(len(widened), 4)
        self.assertEqual(first["drafting"]["drafted_combinations"], 4)
        self.assertEqual(first["drafting"]["space_count"], 8)
        self.assertEqual(first["stages"]["sample"]["combinations"], 4)
        self.assertEqual(first["stages"]["full"]["combinations"], 8)
        self.assertEqual(self._audits("test_plan.drafted")[0].details_json["auto_widened_count"], 4)

        with tenant_session(self.ctx) as session:
            session.execute(delete(AdTestPlanDirectionRecord).where(
                AdTestPlanDirectionRecord.plan_id == accepted["plan_id"]))
            record = session.get(AdTestPlanRecord, accepted["plan_id"])
            record.status = "drafting"
            record.recipe_json = {**record.recipe_json, "factors": [], "constraints": []}
        outcome = asyncio.run(run_plan_drafting(self.ctx, plan_id=accepted["plan_id"], llm_client=self.llm_client))
        self.assertEqual(outcome.kind, "ready")
        self.assertEqual(len(self.openai_requests), 1, "the stored answer replayed; nothing paid twice")
        again = self._get(accepted["plan_id"])
        self.assertTrue(again["drafting"]["call"]["replayed"])
        self.assertEqual(again["drafting"]["auto_widened"], widened)
        self.assertEqual([d["factor_values"] for d in again["directions"]],
                         [d["factor_values"] for d in first["directions"]])
        self.assertEqual(again["stages"]["full"]["combinations"], 8)
        with tenant_session(self.ctx) as session:
            attempts = session.scalars(select(ProviderCallAttemptRecord).where(
                ProviderCallAttemptRecord.workspace_id == self.ctx.workspace_id,
                ProviderCallAttemptRecord.job_id == self._plan_record(accepted["plan_id"]).job_id,
            )).all()
            self.assertEqual(len(attempts), 1)

    def test_another_workspace_cannot_read_the_plan_or_spend_its_call(self):
        self._use_fake_openai(self._answer())
        accepted = self._create({"product_id": self._product()})
        plan = self._plan_record(accepted["plan_id"])
        other = self._make_context(role=ROLE_OWNER, actor="owner_b")

        self.current = other
        response = self.client.get(f"/api/v1/test-plans/{accepted['plan_id']}")
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(Exception) as raised:
            asyncio.run(run_plan_drafting(other, plan_id=accepted["plan_id"], llm_client=self.llm_client))
        self.assertEqual(getattr(raised.exception, "code", None), "test_plan_not_found")

        foreign = LlmPlanDrafter(
            other, llm_client=self.llm_client, job_id=plan.job_id, operation_id=plan.operation_id,
            execution_mode="byok",
        )
        with self.assertRaises(drafting.DraftingFailed) as failed:
            asyncio.run(foreign.draft_plan(_facts(), "explore", _audience(), "sample", drafting.WinnersLosers()))
        self.assertIn(failed.exception.code, {"credential_unavailable", "job_not_found", "reservation_missing"})
        self.assertEqual(self.openai_requests, [], "nothing was sent on the other Workspace's behalf")

        self.current = self.ctx
        self._drain()
        self.assertEqual(self._get(accepted["plan_id"])["status"], "ready")
        self.assertEqual(len(self.openai_requests), 1)


class MockModeDraftingTests(PlanFixtures):
    def test_mock_mode_reserves_nothing_and_keeps_the_mock_drafter(self):
        accepted = self._create({"product_id": self._product()})
        admission = self._get(accepted["plan_id"])
        self._drain()
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["drafting"]["drafter"], "mock")
        self.assertEqual(admission["status"], "drafting")
        with tenant_session(self.ctx) as session:
            self.assertEqual(
                session.get(AdTestPlanRecord, accepted["plan_id"]).evidence_json["admission"],
                {"drafting_mode": "mock", "reserved": False},
            )

    def test_the_llm_drafter_under_mock_maps_the_ev_charger_fixture(self):
        accepted = self._create({"product_id": self._product()})
        with tenant_session(self.ctx) as session:
            record = session.get(AdTestPlanRecord, accepted["plan_id"])
            job_id, operation_id = record.job_id, record.operation_id
        client = fake_openai_llm_client([], [])
        drafter = LlmPlanDrafter(
            self.ctx, llm_client=client, job_id=job_id, operation_id=operation_id, execution_mode="mock",
        )
        outcome = asyncio.run(run_plan_drafting(self.ctx, plan_id=accepted["plan_id"], drafter=drafter))
        self.assertEqual(outcome.kind, "ready")
        plan = self._get(accepted["plan_id"])
        self.assertEqual(plan["drafting"]["drafter"], "llm")
        self.assertEqual(plan["drafting"]["call"]["provider"], "mock")
        self.assertEqual([f["label"] for f in plan["factors"]], ["人物", "场景", "汽车", "钩子"])
        self.assertEqual(plan["directions"][0]["title"], "回家插上，一晚充满")
        self.assertEqual(plan["stages"]["full"]["combinations"], 10)
        with tenant_session(self.ctx) as session:
            self.assertIsNone(session.scalars(select(SpendReservationRecord).where(
                SpendReservationRecord.workspace_id == self.ctx.workspace_id)).first())

    def test_optimize_seeds_carry_readable_dimension_labels(self):
        product_id = self._product()
        plan = self._ready_plan(product_id=product_id)
        sample = self._run(plan["plan_id"], "sample")
        self._drain()
        from app.generation.models import RunItemAssetVersionBindingRecord, RunItemRecord

        with tenant_session(self.ctx) as session:
            seeds = list(session.scalars(
                select(RunItemAssetVersionBindingRecord.asset_version_id)
                .join(RunItemRecord, RunItemRecord.id == RunItemAssetVersionBindingRecord.run_item_id)
                .where(RunItemRecord.generation_run_id == sample["run_id"])
            ))[:1]
        accepted = self._create({"product_id": product_id, "goal": "optimize", "seed_asset_version_ids": seeds})
        with tenant_session(self.ctx) as session:
            winners = session.get(AdTestPlanRecord, accepted["plan_id"]).recipe_json["seeds"]["winners"]
        labels = winners[0]["dimension_labels"]
        self.assertIn("创意方向", labels)
        self.assertIn(labels["创意方向"], [d["title"] for d in plan["directions"]])
        self.assertTrue({"场景", "文案切入", "人物"} <= set(labels))
        recipe = drafting.WinnersLosers.from_dict({"winners": winners})
        prompt = build_user_prompt(_facts(), "optimize", _audience(), "sample", recipe)
        self.assertIn(f"创意方向 = {labels['创意方向']}", prompt)


if __name__ == "__main__":
    unittest.main()
