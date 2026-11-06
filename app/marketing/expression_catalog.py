"""Reference expressions for planning: a starting vocabulary, never a closed enum.

Planners and users may propose any new mechanism as ``other:<description>``.
Distinctness compares normalized mechanisms, so a custom label cannot restate
a catalog mechanism under a new name.
"""
from __future__ import annotations

import re

CATALOG_VERSION = "expression-catalog-v1"
OTHER_PREFIX = "other:"
MAX_OTHER_LENGTH = 80

_SPEC = "MarketAI spec §3–§4 expression examples (2026-10-23 revision)"
_DIRECT_RESPONSE = "Common direct-response creative formats; confirm product applicability"

ENTRIES = (
    {"visual_mechanism": "route_map", "name": "Route visualization",
     "how_to_tell": "Plot a real journey on a map and mark where the product adds options or removes a constraint.",
     "suited_evidence": "Documented range, coverage, compatibility or location facts.", "source": _SPEC},
    {"visual_mechanism": "live_connection_demo", "name": "Live hands-on demonstration",
     "how_to_tell": "Show the product being connected or operated step by step, so the benefit is observed rather than asserted.",
     "suited_evidence": "Documented setup steps, interfaces or operating behaviour.", "source": _SPEC},
    {"visual_mechanism": "scenario_story", "name": "Situation story",
     "how_to_tell": "Tell a short story of a concrete situation where the problem appears and the product resolves it.",
     "suited_evidence": "Documented use situations and the product facts that make the outcome plausible.", "source": _SPEC},
    {"visual_mechanism": "certification_walkthrough", "name": "Certification walkthrough",
     "how_to_tell": "Explain what a verified certification covers and what it means for the buyer, citing the original mark.",
     "suited_evidence": "A confirmed certification fact and a verified original mark; never the product name alone.", "source": _SPEC},
    {"visual_mechanism": "qa_objection", "name": "Question and answer",
     "how_to_tell": "Pose the buyer's actual hesitation as a question and answer it with documented facts.",
     "suited_evidence": "Known purchase objections and documented answers.", "source": _SPEC},
    {"visual_mechanism": "test_process", "name": "Test process",
     "how_to_tell": "Show how a claim is checked: the setup, the measurement and the documented result.",
     "suited_evidence": "A documented test method and result for the exact product.", "source": _DIRECT_RESPONSE},
    {"visual_mechanism": "side_by_side_comparison", "name": "Side-by-side comparison",
     "how_to_tell": "Place the product next to the buyer's current alternative and show one documented difference.",
     "suited_evidence": "Documented differences against a named alternative; no unsupported superiority claims.", "source": _DIRECT_RESPONSE},
    {"visual_mechanism": "owner_testimonial", "name": "Owner story",
     "how_to_tell": "Let a real owner describe the situation and outcome in their own words.",
     "suited_evidence": "A consented, verifiable customer account; never an invented testimonial.", "source": _SPEC},
    {"visual_mechanism": "feature_closeup", "name": "Feature close-up",
     "how_to_tell": "Zoom into the parts that deliver the benefit and label each with one documented fact.",
     "suited_evidence": "Documented physical features or specifications.", "source": _DIRECT_RESPONSE},
    {"visual_mechanism": "fit_guide", "name": "Fit guide",
     "how_to_tell": "Show who and which situations the product suits, and where it does not, as a simple checklist.",
     "suited_evidence": "Documented compatibility, conditions and limits.", "source": _DIRECT_RESPONSE},
)


#: Display names by interface language, kept apart from ENTRIES so the planner's
#: frozen reference (``planner_reference``) and every prompt built from it stay
#: unchanged. They mirror the web's ``marketing-visual-mechanism-<key>`` copy.
FACTOR_NAMES = {"en": "Expression mechanism", "zh-CN": "表达机制"}
DISPLAY_NAMES = {
    "route_map": {"en": "Route visualization", "zh-CN": "路线可视化"},
    "live_connection_demo": {"en": "Live hands-on demonstration", "zh-CN": "现场实操演示"},
    "scenario_story": {"en": "Situation story", "zh-CN": "情境故事"},
    "certification_walkthrough": {"en": "Certification walkthrough", "zh-CN": "认证讲解"},
    "qa_objection": {"en": "Question and answer", "zh-CN": "问答释疑"},
    "test_process": {"en": "Test process", "zh-CN": "测试过程"},
    "side_by_side_comparison": {"en": "Side-by-side comparison", "zh-CN": "并排对比"},
    "owner_testimonial": {"en": "Owner story", "zh-CN": "车主故事"},
    "feature_closeup": {"en": "Feature close-up", "zh-CN": "功能特写"},
    "fit_guide": {"en": "Fit guide", "zh-CN": "适配指南"},
}


def display_name(value: str, language: str) -> str | None:
    """A catalog mechanism's name in ``language`` (English otherwise); None for a custom label."""
    names = DISPLAY_NAMES.get(value)
    return (names.get(language) or names["en"]) if names else None


def mechanism_keys() -> frozenset[str]:
    return frozenset(entry["visual_mechanism"] for entry in ENTRIES)


def valid_mechanism(value: str) -> bool:
    if value.startswith(OTHER_PREFIX):
        return 0 < len(value[len(OTHER_PREFIX):].strip()) <= MAX_OTHER_LENGTH
    return value in mechanism_keys()


def english_name(value: str | None) -> str | None:
    """The mechanism as the English image prompt names it: the catalog name, or a custom label's
    own description. A label not written in English, or no longer in the catalog, has no name here."""
    from .languages import english_text
    if not value:
        return None
    if value.startswith(OTHER_PREFIX):
        label = " ".join(value[len(OTHER_PREFIX):].split())
        return label if label and english_text(label) else None
    return next((entry["name"] for entry in ENTRIES if entry["visual_mechanism"] == value), None)


def normalize_mechanism(value: str) -> str:
    """Comparison form: ``other: Route-Map`` and ``route_map`` are the same mechanism."""
    value = value.strip().casefold()
    if value.startswith(OTHER_PREFIX):
        value = value[len(OTHER_PREFIX):]
    return re.sub(r"[\s_\-]+", " ", value).strip()


def planner_reference() -> dict:
    """Frozen into each planning call so a draft records the vocabulary it saw."""
    return {"version": CATALOG_VERSION, "open_vocabulary": f"{OTHER_PREFIX}<description>",
            "entries": [dict(entry) for entry in ENTRIES]}
