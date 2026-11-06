"""VLM quality check of a generated image (Issue #169): the pure half.

A generated AssetVersion is checked against what its frozen
:class:`~app.generation.creative_recipe.CreativeRecipe` promised: the
product's confirmed must-match items (Library contract §6 -- each with its
own severity and ``qc_method``) and the product's reference images. The
check is one governed ``llm_chat`` vision call (semantic
:data:`QC_SEMANTIC`); this module owns everything about it that needs no
database or network:

* **Policy** (:func:`resolve_qc_policy`). Frozen into the recipe at Preview
  under :data:`QC_RECIPE_KEY`, so the preview's Maximum cost, the
  submission's reservation and the Worker's call agree. ``byok`` runs are
  checked by the Workspace's own OpenAI key (the ``llm_chat`` key, bound in
  Settings); ``mock`` runs by the controlled mock (zero real cost, never
  mixed with real evidence). A ``byok`` run without an OpenAI key gets the
  ``unavailable`` method: nothing is reserved, and every image records an
  ``uncertain`` check that says why -- never a silent pass.
* **Price** (:func:`qc_input_token_ceiling`, :func:`qc_max_cost`). The user
  prompt is cut to :data:`MAX_QC_USER_PROMPT_BYTES`, so the input ceiling is
  a function of the model and the image count only: one generated image plus
  up to :data:`MAX_QC_REFERENCES` references. Admission reserves exactly
  ``call_max_cost`` of that ceiling (answer + one schema retry), and the
  Worker's real request can never exceed it.
* **Request** (:func:`build_qc_request`) and **answer mapping**
  (:func:`interpret_qc_answer`). The answer's per-item verdicts become
  append-only check rows; the attempt's state is derived here from those
  rows (the model's own ``overall`` is kept as evidence only). A missing
  verdict, an item over the per-call cap, or a reference that could not be
  read is ``uncertain`` -- never ``pass``. Each item's ``qc_method`` decides
  how much the verdict weighs: ``automated_check`` / ``reference_diff``
  items are decided by it (a blocking item's fail blocks approval),
  ``human_review`` items are checked as advice (a fail projects Needs
  Changes but a person may still approve), ``none`` items are skipped.
* **Built-in composition checks** (Issue #194, policy ``vlm-qc/v2``). Next
  to the product's must-match items, every image is checked for what no
  product card has to say (:data:`COMPOSITION_CHECKS`): one single
  continuous photograph -- no collage, split screen, panels, before / after
  layout or visible seam -- is *blocking*, with the same weight as an
  ``automated_check`` item; the product shown once unless the scene asks for
  several, and a clean frame without garbled text, watermark or a leftover
  reference-photo border, are advisory. Same call, same answer: only the
  schema and the system prompt grow, so a v2 policy's input ceiling and
  per-image maximum are higher than v1's. A missing answer is ``uncertain``.

**Versions.** The prompt, the schema, the answer mapping and therefore the
price are a function of the frozen policy's ``version``
(:data:`QC_VERSIONS`). A recipe frozen under ``vlm-qc/v1`` reserved v1's
maximum, so its check (a Recovery retry, a redelivered Job) is still built,
priced and interpreted exactly as v1: its own prompt and schema, no
composition checks. New Previews freeze :data:`QC_POLICY_VERSION`.

The controlled mock fixture for :data:`QC_SEMANTIC` passes every item by
default and is steerable from data for Mock demos and e2e: a must-match
description containing ``[mock:qc-fail]`` fails that item,
``[mock:qc-uncertain]`` makes it uncertain, ``[mock:qc-fail]`` in the
product line reports the product as missing, and ``[mock:qc-composite]``
anywhere in the checked text (product, scene or a must-match description)
reports the image as a composite -- the blocking built-in check (v2 only).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any, Mapping, Sequence

from app.generation.creative_recipe import CreativeRecipe, MustMatchItem
from app.providers.llm_registry import (
    LLM_PRICE_RULE_VERSION,
    MOCK_PROVIDER,
    OPENAI_PROVIDER,
    call_max_cost,
    estimate_input_tokens,
    model_spec,
)
from app.providers.llm_mock import register_llm_mock_fixture
from app.providers.llm_types import LlmChatRequest, LlmImage, ResolvedLlmChatRequest

QC_SEMANTIC = "generation_vlm_qc"
QC_SCHEMA_NAME = "generation_vlm_qc"
#: The ``llm_chat`` call name on the RunItem's dispatching Job: its key is
#: ``llm_call_key(<generation job>, QC_CALL_NAME)``, reserved at admission.
QC_CALL_NAME = "vlm_qc"
#: Must-match items + product present + stray text (Issue #169).
QC_POLICY_VERSION_V1 = "vlm-qc/v1"
#: v1 + the built-in composition checks (Issue #194). What new Previews freeze.
QC_POLICY_VERSION_V2 = "vlm-qc/v2"
QC_POLICY_VERSION = QC_POLICY_VERSION_V2
QC_RULE_SET_VERSION = "vlm-qc-rules/v2"
#: Key of the frozen policy inside a recipe snapshot's ``canonical_json``.
QC_RECIPE_KEY = "quality_check"

METHOD_VLM = "vlm"
METHOD_UNAVAILABLE = "unavailable"
#: Recipes frozen before Issue #169 carry no policy: the Worker keeps the old
#: deterministic check for them (nothing was reserved for a VLM call).
METHOD_LEGACY = "deterministic"

#: Reference images compared per check (primary first).
MAX_QC_REFERENCES = 3
#: Must-match items judged per call; the rest are recorded ``uncertain``.
MAX_QC_ITEMS = 12
#: Output ceiling: a Chinese reason and an English hint per item, plus a
#: reasoning model's hidden tokens.
QC_MAX_OUTPUT_TOKENS = 2_000
#: The user prompt is cut to this many UTF-8 bytes (it is the only variable
#: text, so the reservation's input ceiling is fixed per image count).
MAX_QC_USER_PROMPT_BYTES = 6_000
_MAX_FIELD_CHARS = 300
_MICRO = Decimal("0.000001")

#: Generation execution mode -> the ``llm_chat`` mode that checks it.
_LLM_MODE_FOR = {"byok": "byok", "mock": "mock"}

#: Library severities -> ``asset_qc_check_results.severity``.
_SEVERITY = {"blocking": "must_match", "warning": "should_match"}

#: ``qc_method`` values whose item the VLM verdict decides (Library contract
#: §6). ``human_review`` items are still checked, but the verdict is advice:
#: a fail projects Needs Changes with its reason and never blocks a person's
#: approval; ``none`` items are not checked at all.
AUTHORITATIVE_QC_METHODS = ("automated_check", "reference_diff")


def item_severity(item: MustMatchItem) -> str:
    """The check row's severity: blocking only where the VLM is authoritative."""
    severity = _SEVERITY.get(item.severity, "must_match")
    if severity == "must_match" and item.qc_method not in AUTHORITATIVE_QC_METHODS:
        return "should_match"
    return severity

#: Check verdict -> ``asset_qc_check_results.outcome`` (``uncertain`` is an
#: ``error`` row: the check could not be decided).
OUTCOME_FOR_VERDICT = {"pass": "pass", "fail": "fail", "uncertain": "error", "skip": "skip"}

MOCK_FAIL_MARKER = "[mock:qc-fail]"
MOCK_UNCERTAIN_MARKER = "[mock:qc-uncertain]"
MOCK_COMPOSITE_MARKER = "[mock:qc-composite]"


@dataclass(frozen=True)
class CompositionCheck:
    """A built-in check of the generated image no product card configures."""

    key: str  # the answer's property under ``composition``
    rule_id: str
    severity: str  # must_match (blocking) | should_match (advisory)
    label: str  # Chinese, shown before the reason
    fail_reason: str  # Chinese fallback when the model wrote none
    fix_hint: str  # English fallback when the model wrote none


#: Issue #194. Order is the schema's and the check rows' order.
COMPOSITION_CHECKS: tuple[CompositionCheck, ...] = (
    CompositionCheck(
        key="single_image", rule_id="composition:single_image", severity="must_match",
        label="画面完整性：单一连贯画面",
        fail_reason="画面是拼图、分屏或多格组合，或有明显接缝，不能投放。",
        fix_hint=(
            "render one single continuous photograph of one scene: no split screen, no collage, "
            "no side-by-side panels, no visible seam"
        ),
    ),
    CompositionCheck(
        key="product_once", rule_id="composition:product_once", severity="should_match",
        label="画面完整性：产品只出现一次",
        fail_reason="产品在画面中重复出现。",
        fix_hint="show exactly one unit of the product, do not duplicate it",
    ),
    CompositionCheck(
        key="clean_frame", rule_id="composition:clean_frame", severity="should_match",
        label="画面完整性：无乱码、水印或参考图边框",
        fail_reason="画面中有乱码文字、水印或参考图边框残留。",
        fix_hint=(
            "no visible text, watermark, gibberish lettering or leftover reference-photo border "
            "anywhere in the image"
        ),
    ),
)


# ---------------------------------------------------------------------------
# Schema and prompt
# ---------------------------------------------------------------------------


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_VERDICT = {"type": "string", "enum": ["pass", "fail", "uncertain"]}

_CHECK_ANSWER = _object({
    "verdict": _VERDICT,
    "reason": {"type": "string"},
    "fix_hint": {"type": "string"},
})

#: OpenAI strict structured output: every property required, no open objects.
#: v1 (Issue #169) -- frozen: recipes priced under v1 replay with it.
QC_SCHEMA_V1: dict[str, Any] = _object({
    "items": {
        "type": "array",
        "items": _object({
            "id": {"type": "string"},
            "verdict": _VERDICT,
            "reason": {"type": "string"},
            "fix_hint": {"type": "string"},
        }),
    },
    "product_present": {"type": "string", "enum": ["yes", "no", "uncertain"]},
    "product_reason": {"type": "string"},
    "text_artifacts": {"type": "string", "enum": ["none", "present", "uncertain"]},
    "text_reason": {"type": "string"},
    "overall": _VERDICT,
    "summary": {"type": "string"},
})

#: v2 (Issue #194): the built-in composition checks replace ``text_artifacts``
#: (``clean_frame`` covers stray text, watermarks and reference borders).
QC_SCHEMA_V2: dict[str, Any] = _object({
    "items": QC_SCHEMA_V1["properties"]["items"],
    "product_present": QC_SCHEMA_V1["properties"]["product_present"],
    "product_reason": {"type": "string"},
    "composition": _object({check.key: _CHECK_ANSWER for check in COMPOSITION_CHECKS}),
    "overall": _VERDICT,
    "summary": {"type": "string"},
})

#: v1 (Issue #169) -- frozen, like :data:`QC_SCHEMA_V1`.
QC_SYSTEM_PROMPT_V1 = (
    "You are a strict visual quality checker for product advertising images. "
    "The FIRST attached image is the generated ad image under review. Any further images are "
    "reference photos of the real product (the first reference is the primary view). "
    "Judge only what you can see. For every must-match requirement, answer exactly once with its "
    "id: pass if the generated image clearly satisfies it; fail if it clearly violates it (wrong "
    "color, shape, material, logo, missing or extra part, wrong proportions); uncertain if it "
    "cannot be judged (the detail is not visible, the image is unclear, no reference shows it). "
    "Never guess pass. Write every reason in Simplified Chinese, one or two short sentences naming "
    "what you saw. For a fail, write fix_hint as one short English instruction for the image "
    "generator that would fix it (for example: keep the housing matte black, not glossy silver); "
    "otherwise leave fix_hint empty. Also report product_present (is the product itself clearly in "
    "the image: yes, no or uncertain) with product_reason, and text_artifacts (unintended visible "
    "text, watermarks or gibberish lettering, not counting the product's own logo and markings: "
    "none, present or uncertain) with text_reason. overall is fail if any requirement fails or the "
    "product is missing, uncertain if anything could not be judged, otherwise pass. summary is one "
    "Chinese sentence."
)

QC_SYSTEM_PROMPT_V2 = (
    "You are a strict visual quality checker for product advertising images. "
    "The FIRST attached image is the generated ad image under review. Any further images are "
    "reference photos of the real product (the first reference is the primary view); they are "
    "inputs, not part of the ad. "
    "Judge only what you can see. For every must-match requirement, answer exactly once with its "
    "id: pass if the generated image clearly satisfies it; fail if it clearly violates it (wrong "
    "color, shape, material, logo, missing or extra part, wrong proportions); uncertain if it "
    "cannot be judged (the detail is not visible, the image is unclear, no reference shows it). "
    "Never guess pass. Write every reason in Simplified Chinese, one or two short sentences naming "
    "what you saw. For a fail, write fix_hint as one short English instruction for the image "
    "generator that would fix it (for example: keep the housing matte black, not glossy silver); "
    "otherwise leave fix_hint empty. Also report product_present (is the product itself clearly in "
    "the image: yes, no or uncertain) with product_reason. "
    "Then answer the built-in composition checks of the generated image under composition, each "
    "with verdict, reason and fix_hint by the same rules. single_image: pass only if the image is "
    "one single continuous photograph of one scene; fail if it is a collage, a split screen, a grid "
    "or strip of panels, a side-by-side or before-and-after comparison, or two pictures joined "
    "along a visible straight vertical or horizontal seam. product_once: pass if the product "
    "appears exactly once; fail if it is duplicated, unless the requested scene explicitly asks "
    "for several units. clean_frame: pass if there is no garbled or gibberish lettering, no "
    "watermark, no stray caption text and no leftover frame, border or inset of a reference photo "
    "(the product's own logo and markings do not count); fail otherwise. "
    "overall is fail if any requirement or composition check fails or the product is missing, "
    "uncertain if anything could not be judged, otherwise pass. summary is one Chinese sentence."
)


@dataclass(frozen=True)
class QCVersion:
    """What a frozen policy version sends and how its answer is read."""

    policy_version: str
    rule_set_version: str
    system_prompt: str
    schema: dict[str, Any]
    composition_checks: bool


QC_VERSIONS: dict[str, QCVersion] = {
    QC_POLICY_VERSION_V1: QCVersion(
        QC_POLICY_VERSION_V1, "vlm-qc-rules/v1", QC_SYSTEM_PROMPT_V1, QC_SCHEMA_V1, False,
    ),
    QC_POLICY_VERSION_V2: QCVersion(
        QC_POLICY_VERSION_V2, QC_RULE_SET_VERSION, QC_SYSTEM_PROMPT_V2, QC_SCHEMA_V2, True,
    ),
}
#: The current version's prompt and schema (what new Previews are priced for).
QC_SCHEMA = QC_SCHEMA_V2
QC_SYSTEM_PROMPT = QC_SYSTEM_PROMPT_V2


class UnknownQCPolicyVersion(ValueError):
    """A frozen policy names a version this code cannot build or read."""


def qc_version(version: str | None) -> QCVersion:
    """The prompt, schema and answer mapping of a frozen policy ``version``.

    A policy without a version predates versioning and is v1.
    """
    try:
        return QC_VERSIONS[str(version or QC_POLICY_VERSION_V1)]
    except KeyError:
        raise UnknownQCPolicyVersion(f"unknown quality-check policy version {version!r}") from None


def rule_set_version(policy: Mapping[str, Any] | None) -> str:
    """The rule set an attempt under ``policy`` is decided by."""
    if not policy:
        return QC_RULE_SET_VERSION
    return qc_version(policy.get("version")).rule_set_version


def _short(text: str, limit: int = _MAX_FIELD_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _cut_utf8(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def checked_items(recipe: CreativeRecipe) -> tuple[list[MustMatchItem], list[MustMatchItem], list[MustMatchItem]]:
    """``(judged, over_cap, skipped)``: ``qc_method == 'none'`` items are skipped."""
    eligible = [item for item in recipe.must_match if item.qc_method != "none" and item.id]
    skipped = [item for item in recipe.must_match if item.qc_method == "none" and item.id]
    return eligible[:MAX_QC_ITEMS], eligible[MAX_QC_ITEMS:], skipped


def build_user_prompt(
    recipe: CreativeRecipe, *, references_attached: int, references_unreadable: int,
) -> str:
    brief = recipe.product
    product = brief.name or "(unnamed product)"
    if brief.description:
        product += f" — {_short(brief.description)}"
    lines = [f"Product: {_short(product, 400)}"]
    scene = "; ".join(recipe.positive_fragments)
    if recipe.direction is not None and recipe.direction.title:
        scene = f"{recipe.direction.title}: {scene}" if scene else recipe.direction.title
    if scene:
        lines.append(f"Scene that was requested: {_short(scene, 400)}")
    if references_attached:
        lines.append(f"Reference photos attached after the generated image: {references_attached} (primary first).")
    else:
        lines.append("No reference photo is attached; judge the requirements from their descriptions.")
    if references_unreadable:
        lines.append(
            f"{references_unreadable} reference photo(s) of this product could not be attached; "
            "answer uncertain where only they would show a detail."
        )
    judged, _over_cap, _skipped = checked_items(recipe)
    if judged:
        lines.append("Must-match requirements (answer every id exactly once):")
        for item in judged:
            views = ",".join(item.viewpoints) if item.viewpoints else "any"
            lines.append(
                f"- [{_short(item.id, 64)}] type={_short(item.type, 40)} severity={item.severity} "
                f"viewpoints={_short(views, 80)}: {_short(item.description)}"
            )
    else:
        lines.append("There are no must-match requirements: answer items as an empty list.")
    return _cut_utf8("\n".join(lines), MAX_QC_USER_PROMPT_BYTES)


def build_qc_request(
    recipe: CreativeRecipe,
    policy: Mapping[str, Any],
    *,
    generated: LlmImage,
    references: Sequence[LlmImage],
    references_unreadable: int = 0,
) -> LlmChatRequest:
    """The one ``llm_chat`` request: generated image first, then references.

    Built with the frozen policy's version (its prompt and schema), so the
    request never outgrows the maximum that version reserved.
    """
    version = qc_version(policy.get("version"))
    return LlmChatRequest.build(
        semantic=QC_SEMANTIC,
        system=version.system_prompt,
        user=build_user_prompt(
            recipe, references_attached=len(references), references_unreadable=references_unreadable,
        ),
        json_schema=version.schema,
        max_output_tokens=int(policy.get("max_output_tokens") or QC_MAX_OUTPUT_TOKENS),
        model=str(policy.get("model") or "") or None,
        images=(generated, *references),
        schema_name=QC_SCHEMA_NAME,
    )


# ---------------------------------------------------------------------------
# Policy and price
# ---------------------------------------------------------------------------


def _schema_json(schema: Mapping[str, Any]) -> str:
    # The same serialization ``llm_chat`` estimates the schema with.
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def qc_input_token_ceiling(
    provider: str, model: str | None, image_count: int, *, version: str = QC_POLICY_VERSION,
) -> int:
    """Upper bound on one QC request's input tokens (prompt cut, images capped)
    under policy ``version`` (its system prompt and schema)."""
    spec = model_spec(provider, model)
    rules = qc_version(version)
    return estimate_input_tokens(
        spec,
        texts=(rules.system_prompt, "x" * MAX_QC_USER_PROMPT_BYTES),
        schema_json=_schema_json(rules.schema),
        image_count=image_count,
    )


def qc_max_cost(
    provider: str, model: str | None, image_count: int, *, version: str = QC_POLICY_VERSION,
) -> Decimal:
    """Maximum of one QC call (its schema retry included), rounded *up* to a
    micro-dollar: the Run's ``est_cost`` and the confirmation quote keep six
    decimals, and the reservation must never be below what the Worker's
    ``llm_chat`` computes for the real request."""
    spec = model_spec(provider, model)
    exact = call_max_cost(
        spec,
        input_tokens=qc_input_token_ceiling(provider, model, image_count, version=version),
        max_output_tokens=QC_MAX_OUTPUT_TOKENS,
    )
    return exact.quantize(_MICRO, rounding=ROUND_CEILING)


def resolve_qc_policy(
    *,
    execution_mode: str,
    reference_images_available: int,
    openai_key_bound: bool,
    version: str = QC_POLICY_VERSION,
) -> dict:
    """The quality-check policy a Preview freezes for its Run.

    Previews always freeze :data:`QC_POLICY_VERSION`; ``version`` exists so
    an earlier version's frozen policy can be reproduced (replay tests).
    """
    rules = qc_version(version)
    references = max(0, min(int(reference_images_available or 0), MAX_QC_REFERENCES))
    llm_mode = _LLM_MODE_FOR.get(execution_mode)
    if llm_mode is None:
        return {
            "version": rules.policy_version,
            "method": METHOD_UNAVAILABLE,
            "reason": "execution_mode_not_checked",
            "detail": f"{execution_mode} 模式的生成结果不做自动质检，请人工审核。",
        }
    if llm_mode == "byok" and not openai_key_bound:
        return {
            "version": rules.policy_version,
            "method": METHOD_UNAVAILABLE,
            "reason": "openai_key_not_bound",
            "detail": "工作区没有绑定 OpenAI 密钥，生成结果无法自动核对外观，请人工审核。",
        }
    provider = OPENAI_PROVIDER if llm_mode == "byok" else MOCK_PROVIDER
    spec = model_spec(provider, None)
    image_count = 1 + references
    maximum = qc_max_cost(provider, spec.model, image_count, version=rules.policy_version)
    policy = {
        "version": rules.policy_version,
        "method": METHOD_VLM,
        "execution_mode": llm_mode,
        "provider": provider,
        "model": spec.model,
        "reference_images": references,
        "image_count": image_count,
        "input_token_ceiling": qc_input_token_ceiling(
            provider, spec.model, image_count, version=rules.policy_version,
        ),
        "max_output_tokens": QC_MAX_OUTPUT_TOKENS,
        # Real money only for byok; the mock's figure is simulated.
        "max_cost_per_image": format(maximum if llm_mode == "byok" else Decimal("0"), "f"),
        "simulated_cost_per_image": format(maximum, "f") if llm_mode == "mock" else None,
        "currency": "USD",
        "price_rule_version": LLM_PRICE_RULE_VERSION,
    }
    if rules.composition_checks:
        policy["rule_set_version"] = rules.rule_set_version
        policy["composition_checks"] = [check.rule_id for check in COMPOSITION_CHECKS]
    return policy


def policy_of(recipe: Mapping[str, Any] | None) -> dict | None:
    """The frozen policy of a recipe, ``None`` for a recipe frozen before #169."""
    policy = (recipe or {}).get(QC_RECIPE_KEY)
    return dict(policy) if isinstance(policy, Mapping) else None


def is_charged(policy: Mapping[str, Any] | None) -> bool:
    return bool(policy) and policy.get("method") == METHOD_VLM and policy.get("execution_mode") == "byok"


def charged_cost_per_image(policy: Mapping[str, Any] | None) -> Decimal:
    """What one image's check reserves (zero unless the policy is a charged VLM)."""
    if not is_charged(policy):
        return Decimal("0")
    return Decimal(str(policy.get("max_cost_per_image") or "0"))


def capability_decision(policy: Mapping[str, Any]) -> dict:
    """The ``quality_check`` entry of a preview's capability decisions."""
    if policy.get("method") == METHOD_VLM:
        refs = int(policy.get("reference_images") or 0)
        detail = (
            f"每张图生成后由 {policy.get('model')} 按 must-match 与 {refs} 张参考图核对"
            if refs else f"每张图生成后由 {policy.get('model')} 按 must-match 核对（产品没有可用参考图）"
        )
        if policy.get("composition_checks"):
            detail += "，并检查是否为单一连贯画面（拼图、分屏、明显接缝不能通过）"
        if policy.get("execution_mode") == "byok":
            detail += f"，每张最多 {policy.get('max_cost_per_image')} USD。"
        else:
            detail += "（受控 Mock，零真实费用）。"
        return {"capability": "quality_check", "supported": True, "detail": detail}
    return {"capability": "quality_check", "supported": False, "detail": policy.get("detail")}


# ---------------------------------------------------------------------------
# Answer -> verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QCCheck:
    """One append-only check row of a QC attempt."""

    rule_id: str
    severity: str  # must_match | should_match | informational
    verdict: str  # pass | fail | uncertain | skip
    reason: str  # Chinese, shown to people
    fix_hint: str = ""  # English, fed into the next recipe on a fail
    must_match_id: str | None = None
    description: str = ""

    @property
    def outcome(self) -> str:
        return OUTCOME_FOR_VERDICT[self.verdict]

    def evidence(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "fix_hint": self.fix_hint,
            "must_match_id": self.must_match_id,
            "description": self.description,
        }


@dataclass(frozen=True)
class QCVerdict:
    """The outcome of one quality check, ready to record."""

    state: str  # passed | failed_blocking | failed_nonblocking | uncertain
    checks: tuple[QCCheck, ...]
    summary: str
    evidence: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.state in ("failed_blocking", "failed_nonblocking")

    def reasons(self) -> list[str]:
        """Chinese reasons of the failed checks (or of the uncertain ones)."""
        wanted = ("fail",) if self.failed else ("uncertain",)
        return [check.reason for check in self.checks if check.verdict in wanted and check.reason]

    def fix_hints(self) -> list[str]:
        return [check.fix_hint for check in self.checks if check.verdict == "fail" and check.fix_hint]


def derive_state(checks: Sequence[QCCheck]) -> str:
    """Our rule, not the model's ``overall``: a blocking fail blocks approval."""
    if any(c.verdict == "fail" and c.severity == "must_match" for c in checks):
        return "failed_blocking"
    if any(c.verdict == "fail" for c in checks):
        return "failed_nonblocking"
    if any(c.verdict == "uncertain" and c.severity in ("must_match", "should_match") for c in checks):
        return "uncertain"
    return "passed"


def _rule_id(prefix: str, value: str) -> str:
    return f"{prefix}{value}"[:64]


def _clean_reason(text: Any, fallback: str) -> str:
    cleaned = _short(text, 500)
    return cleaned or fallback


def _composition_checks(answer: Mapping[str, Any]) -> list[QCCheck]:
    """The built-in composition checks' rows (Issue #194).

    Each check keeps its own weight whatever the verdict: a composite image
    blocks like a blocking ``automated_check`` item, the other two advise. A
    check the answer does not carry is ``uncertain`` -- never a pass.
    """
    composition = answer.get("composition")
    composition = composition if isinstance(composition, Mapping) else {}
    rows: list[QCCheck] = []
    for check in COMPOSITION_CHECKS:
        entry = composition.get(check.key)
        if not isinstance(entry, Mapping):
            rows.append(QCCheck(
                rule_id=check.rule_id, severity=check.severity, verdict="uncertain",
                reason="模型没有给出这一项的判定，请人工核对。", description=check.label,
            ))
            continue
        verdict = str(entry.get("verdict") or "uncertain")
        if verdict not in ("pass", "fail", "uncertain"):
            verdict = "uncertain"
        fallback = {
            "pass": "符合。", "fail": check.fail_reason,
        }.get(verdict, "无法确认，请人工核对。")
        rows.append(QCCheck(
            rule_id=check.rule_id, severity=check.severity, verdict=verdict,
            reason=_clean_reason(entry.get("reason"), fallback),
            fix_hint=(_short(entry.get("fix_hint"), 200) or check.fix_hint) if verdict == "fail" else "",
            description=check.label,
        ))
    return rows


def interpret_qc_answer(
    answer: Mapping[str, Any],
    recipe: CreativeRecipe,
    *,
    references_unreadable: int = 0,
    policy_version: str = QC_POLICY_VERSION,
) -> QCVerdict:
    """Map a schema-valid answer onto check rows and the attempt state.

    ``policy_version`` is the frozen policy's: a v1 answer is read exactly as
    v1 read it (``text_artifacts``, no composition checks); from v2 on the
    built-in composition checks replace ``text_artifacts``.
    """
    rules = qc_version(policy_version)
    by_id: dict[str, Mapping[str, Any]] = {}
    for entry in answer.get("items") or []:
        if isinstance(entry, Mapping):
            key = str(entry.get("id") or "").strip()
            if key and key not in by_id:
                by_id[key] = entry

    judged, over_cap, skipped = checked_items(recipe)
    checks: list[QCCheck] = []
    for item in judged:
        severity = item_severity(item)
        entry = by_id.get(item.id) or by_id.get(_short(item.id, 64))
        if entry is None:
            checks.append(QCCheck(
                rule_id=_rule_id("must_match:", item.id), severity=severity, verdict="uncertain",
                reason="模型没有给出这一项的判定，请人工核对。", must_match_id=item.id,
                description=item.description,
            ))
            continue
        verdict = str(entry.get("verdict") or "uncertain")
        if verdict not in ("pass", "fail", "uncertain"):
            verdict = "uncertain"
        reason = _clean_reason(entry.get("reason"), "（模型未写理由）")
        if verdict == "pass" and references_unreadable:
            # A pass judged without the reference that shows the detail is
            # not a pass we can stand behind.
            verdict = "uncertain"
            reason = f"参考图未能读取，无法确认：{reason}"
        checks.append(QCCheck(
            rule_id=_rule_id("must_match:", item.id), severity=severity, verdict=verdict,
            reason=reason,
            fix_hint=_short(entry.get("fix_hint"), 200) if verdict == "fail" else "",
            must_match_id=item.id, description=item.description,
        ))
    for item in over_cap:
        checks.append(QCCheck(
            rule_id=_rule_id("must_match:", item.id), severity=item_severity(item),
            verdict="uncertain",
            reason=f"must-match 超过单次自动核对上限（{MAX_QC_ITEMS} 项），这一项请人工核对。",
            must_match_id=item.id, description=item.description,
        ))
    for item in skipped:
        checks.append(QCCheck(
            rule_id=_rule_id("must_match:", item.id), severity="informational", verdict="skip",
            reason="这一项的 QC 方法为 none，不做自动核对。", must_match_id=item.id,
            description=item.description,
        ))

    present = str(answer.get("product_present") or "uncertain")
    present_verdict = {"yes": "pass", "no": "fail"}.get(present, "uncertain")
    checks.append(QCCheck(
        rule_id="product_present", severity="must_match", verdict=present_verdict,
        reason=_clean_reason(
            answer.get("product_reason"),
            {"pass": "画面中有产品。", "fail": "画面中看不到产品。"}.get(present_verdict, "无法确认产品是否在画面中。"),
        ),
        fix_hint=(
            f"show the product {recipe.product.name} clearly as the hero of the image"
            if present_verdict == "fail" else ""
        ),
    ))
    if rules.composition_checks:
        checks.extend(_composition_checks(answer))
    else:
        text = str(answer.get("text_artifacts") or "uncertain")
        text_verdict = {"none": "pass", "present": "fail"}.get(text, "uncertain")
        checks.append(QCCheck(
            rule_id="text_artifacts", severity="should_match" if text_verdict == "fail" else "informational",
            verdict=text_verdict,
            reason=_clean_reason(
                answer.get("text_reason"),
                {"pass": "没有多余文字或水印。", "fail": "画面中有多余文字、水印或乱码。"}.get(
                    text_verdict, "无法确认画面中是否有多余文字。",
                ),
            ),
            fix_hint="no visible text, watermark or lettering anywhere in the image" if text_verdict == "fail" else "",
        ))
    if references_unreadable:
        checks.append(QCCheck(
            rule_id="reference_images", severity="should_match", verdict="uncertain",
            reason=f"{references_unreadable} 张参考图未能读取，外观无法与参考图对照。",
        ))

    state = derive_state(checks)
    summary = _short(answer.get("summary"), 300)
    if not summary:
        summary = {
            "passed": "质检通过。",
            "failed_blocking": "质检未通过（阻断级）。",
            "failed_nonblocking": "质检发现非阻断级问题。",
            "uncertain": "质检无法确认，请人工审核。",
        }[state]
    return QCVerdict(
        state=state,
        checks=tuple(checks),
        summary=summary,
        evidence={
            "model_overall": str(answer.get("overall") or ""),
            "references_unreadable": references_unreadable,
            "items_over_cap": [item.id for item in over_cap],
        },
    )


def unchecked_verdict(reason: str, *, code: str, rule_id: str = "quality_check") -> QCVerdict:
    """An ``uncertain`` outcome for a check that could not run (never a pass)."""
    return QCVerdict(
        state="uncertain",
        checks=(QCCheck(rule_id=rule_id[:64], severity="should_match", verdict="uncertain", reason=reason),),
        summary=reason,
        evidence={"code": code},
    )


# ---------------------------------------------------------------------------
# Controlled mock fixture
# ---------------------------------------------------------------------------

_ITEM_LINE = re.compile(r"^- \[(?P<id>[^\]]+)\] .*?: (?P<description>.*)$")


def mock_qc_answer(request: ResolvedLlmChatRequest) -> dict[str, Any]:
    """Deterministic answer for :data:`QC_SEMANTIC`: pass unless a marker says otherwise."""
    user = next((m.text for m in reversed(request.messages) if m.role == "user"), "")
    items = []
    failed = uncertain = False
    product_missing = False
    for line in user.splitlines():
        if line.startswith("Product:") and MOCK_FAIL_MARKER in line:
            product_missing = True
        match = _ITEM_LINE.match(line)
        if match is None:
            continue
        description = match.group("description")
        if MOCK_FAIL_MARKER in description:
            failed = True
            items.append({
                "id": match.group("id"), "verdict": "fail",
                "reason": "受控 Mock：按场景标记判为与要求不符（不是模型判断）。",
                "fix_hint": f"match the reference exactly: {description.replace(MOCK_FAIL_MARKER, '').strip()}",
            })
        elif MOCK_UNCERTAIN_MARKER in description:
            uncertain = True
            items.append({
                "id": match.group("id"), "verdict": "uncertain",
                "reason": "受控 Mock：按场景标记判为无法确认（不是模型判断）。", "fix_hint": "",
            })
        else:
            items.append({
                "id": match.group("id"), "verdict": "pass",
                "reason": "受控 Mock：判为符合（不是模型判断）。", "fix_hint": "",
            })
    composite = False
    answer: dict[str, Any] = {
        "items": items,
        "product_present": "no" if product_missing else "yes",
        "product_reason": "受控 Mock：按场景标记判为看不到产品。" if product_missing else "受控 Mock：画面中有产品。",
    }
    if "composition" in (request.json_schema.get("properties") or {}):
        # v2 (Issue #194): the built-in composition checks.
        composite = MOCK_COMPOSITE_MARKER in user
        composition = {
            check.key: {"verdict": "pass", "reason": "受控 Mock：判为符合（不是模型判断）。", "fix_hint": ""}
            for check in COMPOSITION_CHECKS
        }
        if composite:
            composition["single_image"] = {
                "verdict": "fail",
                "reason": "受控 Mock：按场景标记判为左右拼接的两张图，中间有竖直接缝（不是模型判断）。",
                "fix_hint": COMPOSITION_CHECKS[0].fix_hint,
            }
        answer["composition"] = composition
    else:
        answer["text_artifacts"] = "none"
        answer["text_reason"] = "受控 Mock：没有多余文字。"
    overall = "fail" if failed or product_missing or composite else ("uncertain" if uncertain else "pass")
    answer["overall"] = overall
    answer["summary"] = f"受控 Mock 质检示例（{len(request.images)} 张图），不是模型判断。"
    return answer


def _mock_fixture(request: ResolvedLlmChatRequest) -> dict[str, Any]:
    if request.schema_name != QC_SCHEMA_NAME:
        from app.providers.llm_mock import synthesize_from_schema

        return synthesize_from_schema(request.json_schema)
    return mock_qc_answer(request)


register_llm_mock_fixture(QC_SEMANTIC, _mock_fixture)
