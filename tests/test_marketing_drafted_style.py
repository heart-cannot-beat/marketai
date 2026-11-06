"""A drafted plan's style fields say what its style_variation means, so a live draft confirms as drafted (#354, spec §6.1)."""
from copy import deepcopy

from app.marketing.contracts import TestPlan as MarketingTestPlan
from app.marketing.plan_review import style_violation, with_style_variation
from app.marketing.planner import example_result
from tests.test_marketing_plan_key_conditions import KeyConditionFixtures

LABEL = {"en-US": "Visual style", "zh-CN": "视觉风格"}
# Abridged from the three live en-US drafts of 2026-11-02 (C-50 #277): each held style in a way confirmation refused.
LIVE = {
    "en-US": {
        "reworded": (["angles", "expressions", "combinations", "groups"],
                     ["ad language en-US", "style variation held", "placement not specified", "currency USD", "media budget 1000"],
                     "held"),
        "unlisted": (["angle", "expression", "headline", "body_copy", "visual_brief", "elements"],
                     ["ad language: en-US", "market: North America", "currency: USD", "media budget"], ""),
        "style_notes": (["angles", "expressions", "combinations", "groups"],
                        ["audience", "market", "ad_language", "media budget", "landing page", "currency", "style notes"], ""),
    },
    "zh-CN": {
        "reworded": (["角度", "表达", "组合", "测试组"], ["广告语言 zh-CN", "风格保持不变", "币种 USD", "媒体预算 1000"], "保持"),
        "unlisted": (["角度", "表达", "标题", "正文", "画面要求"], ["广告语言：zh-CN", "市场：北美", "币种：USD", "媒体预算"], ""),
        "style_notes": (["角度", "表达", "组合", "测试组"], ["受众", "市场", "广告语言", "媒体预算", "落地页", "币种", "风格说明"], ""),
    },
}


def plan(language="en-US", **fields):
    document = example_result("plan", {"understanding": {"audience": "Commuters", "facts": [{"id": "f1"}]},
                                       "strategy_language": language, "ad_language": language})
    return MarketingTestPlan.model_validate({**document, **fields})


def live(language, shape):
    changed, held, style = LIVE[language][shape]
    drafted = plan(language, changed=changed, held_constant=held)
    return drafted.model_copy(update={"groups": [g.model_copy(update={"style": style}) for g in drafted.groups]})


def style_tested(language="en-US", styles=("Bright lifestyle photo", "Dark studio shot")):
    drafted = plan(language, style_variation="tested")
    return drafted.model_copy(update={"groups": [g.model_copy(update={"style": s}) for g, s in zip(drafted.groups, styles)]})


class TestStyleVariationOnDrafts:
    def test_live_held_drafts_list_the_label_and_clear_group_styles(self):
        for language, shapes in LIVE.items():
            for shape, (changed, held, _) in shapes.items():
                drafted = live(language, shape)
                assert style_violation(drafted) is not None, (language, shape)
                normalized = with_style_variation(drafted)
                assert style_violation(normalized) is None, (language, shape)
                # Only the label is added; the model's own wording, including its style items, stays.
                assert normalized.held_constant == [*held, LABEL[language]]
                assert normalized.changed == changed
                assert [g.style for g in normalized.groups] == ["", ""]

    def test_held_drops_a_style_change(self):
        drafted = plan(changed=["Buying proposition", "Visual style per group"])
        normalized = with_style_variation(drafted)
        assert normalized.changed == ["Buying proposition"]
        assert style_violation(normalized) is None

    def test_tested_moves_the_label_to_changed_and_keeps_group_styles(self):
        for language in LIVE:
            drafted = style_tested(language)
            assert style_violation(drafted)[0] == "style_variation_undeclared"
            normalized = with_style_variation(drafted)
            assert LABEL[language] not in normalized.held_constant
            assert normalized.changed == [*drafted.changed, LABEL[language]]
            assert [g.style for g in normalized.groups] == ["Bright lifestyle photo", "Dark studio shot"]
            assert style_violation(normalized) is None

    def test_tested_never_writes_a_missing_group_style(self):
        normalized = with_style_variation(style_tested(styles=("Bright lifestyle photo", "")))
        assert [g.style for g in normalized.groups] == ["Bright lifestyle photo", ""]
        assert style_violation(normalized) == ("group_style_required", ["body.plan.groups.1.style"])

    def test_a_specific_factor_that_tests_another_factor_is_left_to_the_user(self):
        drafted = plan(comparison_kind="specific_factor", changed=["Headline wording"],
                       conclusion_scope="specific_factor", style_variation="tested")
        assert with_style_variation(drafted) == drafted
        assert style_violation(drafted)[0] == "style_variation_conflicts_factor"

    def test_a_result_the_plan_contract_refuses_stays_as_drafted(self):
        # Style is the only declared change, or the held list is already full (20 items).
        for drafted in (plan(changed=["Visual style"]), plan(held_constant=[f"Condition {n}" for n in range(20)])):
            assert with_style_variation(drafted) == drafted
            assert style_violation(drafted)[0] == "style_not_held"

    def test_compliant_plans_and_the_controlled_mock_are_unchanged(self):
        for language in LIVE:
            fixed = plan(language)
            assert style_violation(fixed) is None
            assert with_style_variation(fixed).model_dump() == fixed.model_dump()
            declared = with_style_variation(style_tested(language))
            assert with_style_variation(declared).model_dump() == declared.model_dump()


class TestLiveDraftsConfirmAsDrafted(KeyConditionFixtures):
    # The source supplies the landing page and Offer, so the paid draft asks no key-condition question.
    source = "Alpha 充电器\n65W，多口。Price: $49 with free shipping. Buy at https://shop.example.com/alpha"

    def setUp(self):
        super().setUp()
        self.paid_setup()

    def test_each_live_shape_confirms_without_editing(self):
        for shape, (changed, held, style) in LIVE["en-US"].items():
            with self.subTest(shape):
                project = self.understood()
                output = self.live_output(project, landing_page="https://shop.example.com/alpha", offer="$49, free shipping",
                                          guiding_questions=[], changed=changed, held_constant=held)
                for group in output["groups"]:
                    group["style"] = style
                drafted, _, _ = self.paid_draft(project, output)
                self.assertEqual(drafted["plan"]["held_constant"], [*held, "Visual style"])
                self.assertEqual([g["style"] for g in drafted["plan"]["groups"]], ["", ""])
                confirmed = self.edit(drafted, "confirm_plan", plan=drafted["plan"])
                self.assertEqual(confirmed.status_code, 200, confirmed.text)

    def test_a_plan_the_user_saves_is_checked_as_written(self):
        project = self.drafted()
        plan = deepcopy(project["plan"])
        plan["held_constant"] = ["Same product, offer, audience and delivery period", "style variation held"]
        plan["groups"][0]["style"] = "held"
        saved = self.edit(project, "save_plan", plan=plan)
        self.assertEqual(saved.status_code, 200, saved.text)
        stored = saved.json()["plan"]
        self.assertEqual(stored["held_constant"], plan["held_constant"])
        self.assertEqual(stored["groups"][0]["style"], "held")
        refused = self.edit(saved.json(), "confirm_plan", plan=stored)
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual(refused.json()["code"], "style_not_held")
        relabelled = {**stored, "held_constant": [*stored["held_constant"], "Visual style"]}
        refused = self.edit(saved.json(), "confirm_plan", plan=relabelled)
        self.assertEqual(refused.json()["code"], "group_style_not_held")
        self.assertEqual([e["field"] for e in refused.json()["field_errors"]], ["body.plan.groups.0.style"])
