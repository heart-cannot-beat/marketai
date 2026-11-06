"""Conservative script checks and read-only projections for historical plans."""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

from .plan_review import BRIEF_BASIS

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
LATIN = re.compile(r"[A-Za-z\u00c0-\u024f]")
LATIN_LANGUAGES = {"en", "fr", "de", "es", "pt", "it", "nl", "sv", "da", "no", "fi", "pl", "tr", "vi", "id", "ms", "cs", "ro", "hu"}


def infer_ad_language(market):
    market = (market or "").casefold()
    for tokens, language in [(("北美", "north america", "united states", "usa", "us", "canada"), "en-US"),
        (("中国", "china", "中文"), "zh-CN"), (("日本", "japan"), "ja-JP"),
        (("韩国", "korea"), "ko-KR"), (("france", "法国"), "fr-FR"),
        (("germany", "德国"), "de-DE"), (("spain", "西班牙"), "es-ES")]:
        if any(token == market or ((len(token) > 2 or not token.isascii()) and token in market) for token in tokens):
            return language
    return "en-US"


def output_language(plan, requested=None):
    """The language AI QC and result explanations write in (J-43): the round plan's strategy language, else the
    requester's interface language. Neither present is a client and plan from before both existed: the old Chinese prompts."""
    return plan.get("strategy_language") or requested or "zh-CN"


def plan_projection(value):
    if value is None:
        return None
    result = deepcopy(value)
    result.pop(BRIEF_BASIS, None)
    if not result.get("ad_language"):
        result.update(ad_language=infer_ad_language(result.get("market")), language_confirmation_required=True)
    result.setdefault("strategy_language", "zh-CN")
    return result


def script_matches(text, language):
    """Script validation only; QC checks actual image language and legibility."""
    prefix = language.split("-")[0].lower()
    cjk, latin = len(CJK.findall(text)), len(LATIN.findall(text))
    if prefix in {"zh", "ja", "ko"}:
        return cjk > 0 and cjk >= latin
    if prefix in LATIN_LANGUAGES:
        return latin > 0 and cjk == 0 and not any(
            char.isalpha() and "LATIN" not in unicodedata.name(char, "") for char in text)
    # Other BCP-47 languages remain available; do not claim script validation.
    return True


def english_text(text):
    return not any(char.isalpha() for char in text) or script_matches(text, "en-US")


def guidance_matches(text, language):
    """Revision guidance travels beside the English image brief; for a Latin-script ad it must bring no other script."""
    return language.split("-")[0].lower() not in LATIN_LANGUAGES or english_text(text)


def english_brief_matches(combo):
    if combo.image_brief is None:
        return False
    brief = combo.image_brief
    return all(english_text(text) for text in [combo.visual_brief,
        *combo.elements.keys(), *combo.elements.values(), brief.proposition, brief.expression,
        *brief.hold_constant, *brief.checks])


def style_brief_fields(plan):
    """Style notes reach the image model verbatim, so they are English like the rest of the brief."""
    return [field for field, text in [("body.plan.style_notes", plan.style_notes),
        *((f"body.plan.groups.{i}.style", g.style) for i, g in enumerate(plan.groups))] if not english_text(text)]


def legacy_plan_projection(value):
    """Retain the pre-language wire shape for already deployed strict clients."""
    if value is None:
        return None
    result = deepcopy(value)
    for key in ("ad_language", "strategy_language", "language_confirmation_required", BRIEF_BASIS):
        result.pop(key, None)
    for combo in result.get("combinations", []):
        combo.pop("image_brief", None)
    return result
