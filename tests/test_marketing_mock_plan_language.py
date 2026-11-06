"""Controlled Mock plans write their defaults in the strategy language (#286)."""
import re

import pytest

from app.marketing.plan_review import PENDING
from app.marketing.planner import example_result

CJK = re.compile(r"[　-〿㐀-䶿一-鿿＀-￯]")


def _strings(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _strings(child, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def _mock_plan(strategy_language, **inputs):
    understanding = example_result("understanding", {
        "source_text": "Acme bottle\nKeeps water cold for 24 hours.", "strategy_language": strategy_language})
    plan = example_result("plan", {
        "understanding": understanding, "strategy_language": strategy_language, "ad_language": "en-US", **inputs})
    return understanding, plan


@pytest.mark.parametrize("inputs", [{}, {"big_round_limits": {
    "currency": "EUR", "media_budget": "500", "max_small_rounds": 2, "remaining_budget": "80"}}])
def test_en_us_mock_understanding_and_plan_have_no_chinese(inputs):
    understanding, plan = _mock_plan("en-US", **inputs)
    assert [(path, text) for path, text in _strings(understanding) if CJK.search(text)] == []
    assert [(path, text) for path, text in _strings(plan) if CJK.search(text)] == []


@pytest.mark.parametrize("language", ["en-US", "zh-CN"])
def test_mock_plan_pending_offer_and_landing_page_are_plan_review_pending(language):
    _, plan = _mock_plan(language)
    assert plan["offer"] == PENDING["offer"][language]
    assert plan["landing_page"] == PENDING["landing_page"][language]
