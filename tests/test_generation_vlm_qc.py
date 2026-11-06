"""Issue #169 -- VLM quality check against must-match items and reference images.

* Pure: a fake VLM answer maps onto per-item verdicts and the attempt state
  by our rules (a blocking fail blocks, a warning fail does not, a missing
  verdict / an item over the cap / an unreadable reference is uncertain --
  never a pass); the policy and its price are fixed per image count and
  cover the longest request the Worker can build.
* Governance: a byok Run reserves each image's check next to the image call
  (same command, same guardrail pass); the Worker's call sends the generated
  image first and then the references, and settles the reservation at the
  reported usage. Mock runs use the controlled mock (zero real cost).
* Outcome: a fail keeps the image and the evidence, fails the RunItem when
  blocking, projects Needs Changes with Chinese reasons (asset detail shows
  them) and feeds the English fix hints into the next recipe (Recovery retry
  and regeneration); the third consecutive failure projects Rejected.
* Tenancy: another Workspace can neither run the check on this Workspace's
  version nor make it read a foreign reference blob.
* Issue #194: the built-in composition checks (single continuous image --
  blocking; product once and clean frame -- advisory) ride in the same call;
  a missing one is uncertain; a recipe frozen under ``vlm-qc/v1`` replays
  with v1's prompt, schema, mapping and price.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from app.adapters.persistence.database import tenant_session
from app.adapters.persistence.models import (
    AssetVersionRecord,
    AuditEventRecord,
    GenerationRunRecord,
    JobRecord,
    QualityReviewRecord,
    new_id,
)
from app.entitlements.cost_models import (
    ProviderCallAttemptRecord,
    ProviderCostLedgerEntryRecord,
    SpendReservationRecord,
)
from app.generation.creative_recipe import (
    CreativeRecipe,
    MustMatchItem,
    OutputFormat,
    ProductBrief,
    build_creative_recipe,
)
from app.generation.models import (
    AssetQCAttemptRecord,
    AssetQCCheckResultRecord,
    GenerationRecipeSnapshotRecord,
    RunItemAssetVersionBindingRecord,
    RunItemRecord,
)
from app.generation.prompting import compose_text_prompt
from app.generation.vlm_qc import (
    COMPOSITION_CHECKS,
    MAX_QC_ITEMS,
    MAX_QC_REFERENCES,
    QC_CALL_NAME,
    QC_MAX_OUTPUT_TOKENS,
    QC_POLICY_VERSION,
    QC_POLICY_VERSION_V1,
    QC_SCHEMA_V1,
    QC_SCHEMA_V2,
    QC_SYSTEM_PROMPT_V1,
    QC_SYSTEM_PROMPT_V2,
    UnknownQCPolicyVersion,
    build_qc_request,
    capability_decision,
    interpret_qc_answer,
    qc_input_token_ceiling,
    qc_max_cost,
    resolve_qc_policy,
)
from app.providers.llm_chat import LlmChatClient, estimate_request_input_tokens, llm_call_key
from app.providers.llm_mock import MockLlmChatAdapter
from app.providers.llm_registry import model_spec, usage_cost
from app.providers.llm_types import LlmImage
from tests import test_creative_recipe as recipe_tests
from tests.test_creative_recipe import PNG, PROTECTOR, REF_FRONT, REF_SIDE
from tests.test_generation_byok import _ScriptedAdapter
from tests.test_llm_chat import USAGE, _completion, _Script
from tests.support.library_fixtures import LibraryFixtures
from tests.support.queue_hygiene import cancel_workspace_jobs, quarantine_foreign_jobs

import unittest


def _composition(**overrides) -> dict:
    """A v2 ``composition`` answer: every built-in check passes unless overridden."""
    answer = {
        check.key: {"verdict": "pass", "reason": "画面完整。", "fix_hint": ""} for check in COMPOSITION_CHECKS
    }
    answer.update(overrides)
    return answer


def _answer(items=(), *, present="yes", overall="pass", composition=None) -> dict:
    """A v2 answer (Issue #194): must-match items, product, composition checks."""
    return {
        "items": list(items),
        "product_present": present,
        "product_reason": "产品在画面中央。" if present == "yes" else "看不到产品。",
        "composition": _composition() if composition is None else composition,
        "overall": overall,
        "summary": "核对完成。",
    }


def _answer_v1(items=(), *, present="yes", text="none", overall="pass") -> dict:
    """A v1 answer (Issue #169), as recipes frozen under ``vlm-qc/v1`` get it."""
    return {
        "items": list(items),
        "product_present": present,
        "product_reason": "产品在画面中央。" if present == "yes" else "看不到产品。",
        "text_artifacts": text,
        "text_reason": "没有多余文字。",
        "overall": overall,
        "summary": "核对完成。",
    }


#: Issue #194: v1 prices as they were frozen before the composition checks
#: (GPT-5 mini, 1..4 images). A v1 recipe must keep replaying at exactly these.
V1_MAX_COST = {1: Decimal("0.014105"), 2: Decimal("0.016105"), 3: Decimal("0.018105"), 4: Decimal("0.020105")}
V1_INPUT_CEILING = {1: 12209, 2: 16209, 3: 20209, 4: 24209}


def _item(id_: str, verdict: str, reason: str = "", hint: str = "") -> dict:
    return {"id": id_, "verdict": verdict, "reason": reason or f"{id_} 判定 {verdict}", "fix_hint": hint}


def _recipe(*must_match: MustMatchItem) -> CreativeRecipe:
    return CreativeRecipe(
        product=ProductBrief(name="VoltGo 65W Charger", description="Compact GaN wall charger"),
        format=OutputFormat(aspect_ratio="1:1"),
        positive_fragments=("a desk by a window",),
        must_match=must_match,
    )


COLOR = MustMatchItem(
    id="mm-color", type="color", description="Matte black housing", severity="blocking", qc_method="reference_diff",
)
ADVISORY = MustMatchItem(
    id="mm-shape", type="shape", description="Rounded corners", severity="blocking", qc_method="human_review",
)
LOGO = MustMatchItem(id="mm-logo", type="logo", description="Logo on the top-left", severity="warning")
NONE = MustMatchItem(id="mm-free", type="other", description="Anything goes", qc_method="none")


# ---------------------------------------------------------------------------
# Pure: answer -> verdicts, policy -> price
# ---------------------------------------------------------------------------


class TestAnswerMapping(unittest.TestCase):
    def test_every_item_passing_passes(self):
        verdict = interpret_qc_answer(
            _answer([_item("mm-color", "pass"), _item("mm-logo", "pass")]), _recipe(COLOR, LOGO, NONE),
        )
        self.assertEqual(verdict.state, "passed")
        by_rule = {check.rule_id: check for check in verdict.checks}
        self.assertEqual(by_rule["must_match:mm-color"].outcome, "pass")
        self.assertEqual(by_rule["must_match:mm-color"].severity, "must_match")
        self.assertEqual(by_rule["must_match:mm-logo"].severity, "should_match")
        self.assertEqual(by_rule["must_match:mm-free"].verdict, "skip")
        self.assertEqual(by_rule["product_present"].verdict, "pass")
        # v2: the built-in composition checks replace text_artifacts.
        self.assertNotIn("text_artifacts", by_rule)
        self.assertEqual(
            [(by_rule[c.rule_id].verdict, by_rule[c.rule_id].severity) for c in COMPOSITION_CHECKS],
            [("pass", "must_match"), ("pass", "should_match"), ("pass", "should_match")],
        )

    def test_a_blocking_fail_blocks_and_keeps_its_reason_and_hint(self):
        verdict = interpret_qc_answer(
            _answer(
                [_item("mm-color", "fail", "外壳是亮银色，不是哑光黑。", "keep the housing matte black"),
                 _item("mm-logo", "pass")],
                overall="fail",
            ),
            _recipe(COLOR, LOGO),
        )
        self.assertEqual(verdict.state, "failed_blocking")
        self.assertEqual(verdict.reasons(), ["外壳是亮银色，不是哑光黑。"])
        self.assertEqual(verdict.fix_hints(), ["keep the housing matte black"])
        check = next(c for c in verdict.checks if c.rule_id == "must_match:mm-color")
        self.assertEqual((check.outcome, check.must_match_id), ("fail", "mm-color"))

    def test_a_warning_fail_or_stray_text_does_not_block(self):
        warning = interpret_qc_answer(
            _answer([_item("mm-color", "pass"), _item("mm-logo", "fail", hint="logo top-left")]),
            _recipe(COLOR, LOGO),
        )
        self.assertEqual(warning.state, "failed_nonblocking")
        text = interpret_qc_answer(
            _answer([_item("mm-color", "pass")], composition=_composition(
                clean_frame={"verdict": "fail", "reason": "右下角有乱码水印。", "fix_hint": ""},
            )),
            _recipe(COLOR),
        )
        self.assertEqual(text.state, "failed_nonblocking")
        self.assertEqual(text.reasons(), ["右下角有乱码水印。"])
        self.assertIn("no visible text", text.fix_hints()[0])

    def test_a_human_review_item_is_checked_but_only_advises(self):
        verdict = interpret_qc_answer(
            _answer([_item("mm-shape", "fail", "四角是直角。", "rounded corners")]), _recipe(ADVISORY),
        )
        # Needs Changes with the reason, but a person may still approve it.
        self.assertEqual(verdict.state, "failed_nonblocking")
        self.assertEqual(verdict.reasons(), ["四角是直角。"])
        check = next(c for c in verdict.checks if c.rule_id == "must_match:mm-shape")
        self.assertEqual(check.severity, "should_match")

    def test_a_missing_product_is_a_blocking_failure(self):
        verdict = interpret_qc_answer(_answer([], present="no"), _recipe())
        self.assertEqual(verdict.state, "failed_blocking")
        self.assertIn("VoltGo 65W Charger", verdict.fix_hints()[0])

    def test_missing_verdicts_and_items_over_the_cap_are_uncertain_never_pass(self):
        many = tuple(
            MustMatchItem(id=f"mm-{i}", type="detail", description=f"detail {i}") for i in range(MAX_QC_ITEMS + 2)
        )
        answered = [_item(f"mm-{i}", "pass") for i in range(MAX_QC_ITEMS - 1)]  # one judged item unanswered
        verdict = interpret_qc_answer(_answer(answered), _recipe(*many))
        self.assertEqual(verdict.state, "uncertain")
        uncertain = [c.must_match_id for c in verdict.checks if c.verdict == "uncertain"]
        self.assertEqual(uncertain, [f"mm-{MAX_QC_ITEMS - 1}", f"mm-{MAX_QC_ITEMS}", f"mm-{MAX_QC_ITEMS + 1}"])
        self.assertEqual(
            {c.outcome for c in verdict.checks if c.verdict == "uncertain"}, {"error"},
        )

    def test_an_unreadable_reference_turns_passes_uncertain_but_keeps_fails(self):
        passed = interpret_qc_answer(_answer([_item("mm-color", "pass")]), _recipe(COLOR), references_unreadable=1)
        self.assertEqual(passed.state, "uncertain")
        self.assertTrue(any(c.rule_id == "reference_images" for c in passed.checks))
        failed = interpret_qc_answer(
            _answer([_item("mm-color", "fail", hint="matte black")]), _recipe(COLOR), references_unreadable=1,
        )
        self.assertEqual(failed.state, "failed_blocking")

    def test_the_models_overall_is_evidence_not_the_verdict(self):
        verdict = interpret_qc_answer(_answer([_item("mm-color", "pass")], overall="fail"), _recipe(COLOR))
        self.assertEqual(verdict.state, "passed")
        self.assertEqual(verdict.evidence["model_overall"], "fail")


class TestCompositionChecks(unittest.TestCase):
    """Issue #194: the built-in checks no product card has to configure."""

    def test_a_composite_image_is_a_blocking_failure_with_a_chinese_reason_and_a_fix_hint(self):
        verdict = interpret_qc_answer(
            _answer([_item("mm-color", "pass")], overall="fail", composition=_composition(single_image={
                "verdict": "fail", "reason": "左右两张图拼在一起，中间有竖直接缝。",
                "fix_hint": "one single photo of the garage scene only, no split screen",
            })),
            _recipe(COLOR),
        )
        # The same weight as a blocking automated_check item.
        self.assertEqual(verdict.state, "failed_blocking")
        self.assertEqual(verdict.reasons(), ["左右两张图拼在一起，中间有竖直接缝。"])
        self.assertEqual(verdict.fix_hints(), ["one single photo of the garage scene only, no split screen"])
        check = next(c for c in verdict.checks if c.rule_id == "composition:single_image")
        self.assertEqual((check.outcome, check.severity, check.must_match_id), ("fail", "must_match", None))
        # The label tells a reviewer which built-in check spoke.
        self.assertIn("单一连贯画面", check.description)

    def test_a_composite_fail_without_a_hint_or_reason_gets_ours(self):
        verdict = interpret_qc_answer(
            _answer(composition=_composition(single_image={"verdict": "fail", "reason": "", "fix_hint": ""})),
            _recipe(),
        )
        self.assertEqual(verdict.state, "failed_blocking")
        self.assertIn("拼图", verdict.reasons()[0])
        self.assertIn("no split screen, no collage, no side-by-side panels", verdict.fix_hints()[0])

    def test_a_duplicated_product_or_a_dirty_frame_only_advises(self):
        for key in ("product_once", "clean_frame"):
            with self.subTest(check=key):
                verdict = interpret_qc_answer(
                    _answer([_item("mm-color", "pass")], composition=_composition(**{
                        key: {"verdict": "fail", "reason": "画面里出现了两个充电器。", "fix_hint": ""},
                    })),
                    _recipe(COLOR),
                )
                self.assertEqual(verdict.state, "failed_nonblocking")
                self.assertEqual(verdict.reasons(), ["画面里出现了两个充电器。"])
                self.assertTrue(verdict.fix_hints()[0])

    def test_a_missing_composition_answer_is_uncertain_never_pass(self):
        whole = _answer([_item("mm-color", "pass")])
        del whole["composition"]
        partial = _answer([_item("mm-color", "pass")])
        del partial["composition"]["single_image"]
        odd = _answer([_item("mm-color", "pass")], composition=_composition(
            single_image={"verdict": "maybe", "reason": "", "fix_hint": ""},
        ))
        for name, answer in (("whole", whole), ("partial", partial), ("odd", odd)):
            with self.subTest(answer=name):
                verdict = interpret_qc_answer(answer, _recipe(COLOR))
                self.assertEqual(verdict.state, "uncertain")
                check = next(c for c in verdict.checks if c.rule_id == "composition:single_image")
                self.assertEqual((check.verdict, check.outcome), ("uncertain", "error"))
                self.assertTrue(check.reason)

    def test_an_uncertain_advisory_check_is_never_a_pass(self):
        verdict = interpret_qc_answer(
            _answer(composition=_composition(product_once={"verdict": "uncertain", "reason": "", "fix_hint": ""})),
            _recipe(),
        )
        self.assertEqual(verdict.state, "uncertain")

    def test_the_request_is_one_call_with_the_composition_checks_in_its_prompt_and_schema(self):
        policy = resolve_qc_policy(execution_mode="byok", reference_images_available=3, openai_key_bound=True)
        self.assertEqual(policy["version"], QC_POLICY_VERSION)
        self.assertEqual(policy["rule_set_version"], "vlm-qc-rules/v2")
        self.assertEqual(policy["composition_checks"], [c.rule_id for c in COMPOSITION_CHECKS])
        request = build_qc_request(
            _recipe(COLOR), policy, generated=LlmImage.from_bytes(PNG), references=[LlmImage.from_bytes(REF_FRONT)],
        )
        system = request.messages[0].text
        self.assertEqual(system, QC_SYSTEM_PROMPT_V2)
        for phrase in ("single_image", "collage", "split screen", "seam", "product_once", "clean_frame"):
            self.assertIn(phrase, system)
        self.assertEqual(request.json_schema, QC_SCHEMA_V2)
        self.assertEqual(
            sorted(QC_SCHEMA_V2["properties"]["composition"]["required"]), sorted(c.key for c in COMPOSITION_CHECKS),
        )
        self.assertNotIn("text_artifacts", QC_SCHEMA_V2["properties"])
        self.assertIn("单一连贯画面", capability_decision(policy)["detail"])

    def test_the_v2_price_covers_the_bigger_prompt_and_schema(self):
        for images in range(1, MAX_QC_REFERENCES + 2):
            with self.subTest(images=images):
                self.assertGreater(
                    qc_input_token_ceiling("openai", "gpt-5-mini", images),
                    qc_input_token_ceiling("openai", "gpt-5-mini", images, version=QC_POLICY_VERSION_V1),
                )
                self.assertGreater(
                    qc_max_cost("openai", "gpt-5-mini", images),
                    qc_max_cost("openai", "gpt-5-mini", images, version=QC_POLICY_VERSION_V1),
                )


class TestV1Replay(unittest.TestCase):
    """A recipe frozen under ``vlm-qc/v1`` replays exactly as v1 (Issue #194)."""

    def test_the_v1_policy_and_price_are_unchanged(self):
        for images, maximum in V1_MAX_COST.items():
            self.assertEqual(qc_max_cost("openai", "gpt-5-mini", images, version=QC_POLICY_VERSION_V1), maximum)
            self.assertEqual(
                qc_input_token_ceiling("openai", "gpt-5-mini", images, version=QC_POLICY_VERSION_V1),
                V1_INPUT_CEILING[images],
            )
        policy = resolve_qc_policy(
            execution_mode="byok", reference_images_available=2, openai_key_bound=True, version=QC_POLICY_VERSION_V1,
        )
        # The exact shape Previews froze before Issue #194.
        self.assertEqual(policy, {
            "version": "vlm-qc/v1", "method": "vlm", "execution_mode": "byok", "provider": "openai",
            "model": "gpt-5-mini", "reference_images": 2, "image_count": 3, "input_token_ceiling": 20209,
            "max_output_tokens": 2000, "max_cost_per_image": "0.018105", "simulated_cost_per_image": None,
            "currency": "USD", "price_rule_version": "openai-chat-2026-10",
        })
        self.assertNotIn("单一连贯画面", capability_decision(policy)["detail"])

    def test_a_v1_request_uses_the_v1_prompt_and_schema_and_fits_the_v1_ceiling(self):
        policy = resolve_qc_policy(
            execution_mode="byok", reference_images_available=3, openai_key_bound=True, version=QC_POLICY_VERSION_V1,
        )
        huge = tuple(
            MustMatchItem(id=f"mm-{i}-" + "x" * 80, type="颜色" * 30, description="非常详细的外观要求" * 80)
            for i in range(MAX_QC_ITEMS + 5)
        )
        recipe = CreativeRecipe(
            product=ProductBrief(name="产品" * 200, description="描述" * 500),
            format=OutputFormat(), positive_fragments=("scene " * 500,), must_match=huge,
        )
        request = build_qc_request(
            recipe, policy, generated=LlmImage.from_bytes(PNG), references=[LlmImage.from_bytes(REF_FRONT)] * 3,
        )
        self.assertEqual(request.messages[0].text, QC_SYSTEM_PROMPT_V1)
        self.assertEqual(request.json_schema, QC_SCHEMA_V1)
        self.assertLessEqual(
            estimate_request_input_tokens(request, execution_mode="byok"), policy["input_token_ceiling"],
        )

    def test_a_v1_answer_maps_exactly_as_before(self):
        verdict = interpret_qc_answer(
            _answer_v1([_item("mm-color", "pass")], text="present"), _recipe(COLOR),
            policy_version=QC_POLICY_VERSION_V1,
        )
        self.assertEqual(verdict.state, "failed_nonblocking")
        self.assertEqual(
            [c.rule_id for c in verdict.checks], ["must_match:mm-color", "product_present", "text_artifacts"],
        )
        self.assertIn("no visible text", verdict.fix_hints()[0])
        # No composition rows, and their absence is not turned into uncertain.
        passed = interpret_qc_answer(
            _answer_v1([_item("mm-color", "pass")]), _recipe(COLOR), policy_version=QC_POLICY_VERSION_V1,
        )
        self.assertEqual(passed.state, "passed")
        self.assertFalse(any(c.rule_id.startswith("composition:") for c in passed.checks))

    def test_an_unknown_policy_version_is_refused_not_guessed(self):
        with self.assertRaises(UnknownQCPolicyVersion):
            build_qc_request(
                _recipe(), {"version": "vlm-qc/v99"}, generated=LlmImage.from_bytes(PNG), references=[],
            )
        with self.assertRaises(UnknownQCPolicyVersion):
            interpret_qc_answer(_answer(), _recipe(), policy_version="vlm-qc/v99")


class TestPolicyAndPrice(unittest.TestCase):
    def test_policy_by_mode(self):
        byok = resolve_qc_policy(execution_mode="byok", reference_images_available=5, openai_key_bound=True)
        self.assertEqual((byok["method"], byok["execution_mode"], byok["model"]), ("vlm", "byok", "gpt-5-mini"))
        self.assertEqual(byok["reference_images"], MAX_QC_REFERENCES)
        self.assertEqual(Decimal(byok["max_cost_per_image"]), qc_max_cost("openai", "gpt-5-mini", 1 + MAX_QC_REFERENCES))
        mock = resolve_qc_policy(execution_mode="mock", reference_images_available=1, openai_key_bound=False)
        self.assertEqual((mock["method"], mock["execution_mode"], mock["max_cost_per_image"]), ("vlm", "mock", "0"))
        self.assertGreater(Decimal(mock["simulated_cost_per_image"]), 0)
        keyless = resolve_qc_policy(execution_mode="byok", reference_images_available=1, openai_key_bound=False)
        self.assertEqual((keyless["method"], keyless["reason"]), ("unavailable", "openai_key_not_bound"))

    def test_the_reserved_ceiling_covers_the_longest_request(self):
        policy = resolve_qc_policy(execution_mode="byok", reference_images_available=3, openai_key_bound=True)
        huge = tuple(
            MustMatchItem(id=f"mm-{i}-" + "x" * 80, type="颜色" * 30, description="非常详细的外观要求" * 80,
                          viewpoints=("front", "side", "back"))
            for i in range(MAX_QC_ITEMS + 5)
        )
        recipe = CreativeRecipe(
            product=ProductBrief(name="产品" * 200, description="描述" * 500),
            format=OutputFormat(), positive_fragments=("scene " * 500,), must_match=huge,
        )
        request = build_qc_request(
            recipe, policy,
            generated=LlmImage.from_bytes(PNG),
            references=[LlmImage.from_bytes(REF_FRONT)] * 3,
        )
        self.assertLessEqual(
            estimate_request_input_tokens(request, execution_mode="byok"), policy["input_token_ceiling"],
        )
        self.assertEqual(request.max_output_tokens, QC_MAX_OUTPUT_TOKENS)
        self.assertEqual(len(request.images), 4)


# ---------------------------------------------------------------------------
# Chain: admission -> image -> quality check -> projection
# ---------------------------------------------------------------------------


MUST_MATCH = [
    {"id": "mm-color", "type": "color", "description": "Matte black housing", "severity": "blocking",
     "viewpoints": ["front"], "qc_method": "reference_diff", "proposed_by": "user"},
    {"id": "mm-logo", "type": "logo", "description": "Logo on the top-left", "severity": "warning",
     "viewpoints": ["front"], "qc_method": "human_review", "proposed_by": "user"},
]


class VlmQualityCheckChainTests(LibraryFixtures):
    # The Issue #167 chain fixtures: entitled owner, in-memory bucket,
    # published ProductVersion with bound reference derivatives.
    setUp = recipe_tests.RecipeChainTests.setUp
    _library_version = recipe_tests.RecipeChainTests._library_version
    _product_with_references = recipe_tests.RecipeChainTests._product_with_references
    _bind = recipe_tests.RecipeChainTests._bind
    _item = recipe_tests.RecipeChainTests._item

    # -- helpers ----------------------------------------------------------

    def _run(self, product_id: str, *, mode: str = "byok", provider: str = "openai",
             model: str = "gpt-image-2") -> tuple[str, str]:
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation

        with tenant_session(self.ctx) as s:
            draft = create_draft(
                s, self.ctx, product_id=product_id, provider=provider, model=model, execution_mode=mode,
                combination_strategy={"kind": "full", "planner_version": "v1"},
                format_config={"aspect_ratio": "1:1", "variants_per_combination": 1},
            )
            preview = create_preview(s, self.ctx, draft.id, draft.revision)
            run_id = submit_generation(
                s, self.ctx, preview_id=preview.id, preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision, idempotency_key=new_id("ik"),
            )["generation_run_id"]
            return run_id, self._item(s, run_id).id

    def _dispatch_byok(self, item_id: str) -> str:
        from app.generation.byok_dispatch import handle_run_item_byok

        result = asyncio.run(handle_run_item_byok(
            self.coordinator, self.ctx, item_id, adapters={"openai": _ScriptedAdapter(PNG)},
            blob_store=self.blob_store, protector=PROTECTOR, session_factory=tenant_session,
        ))
        self.assertTrue(result.success, result.error_detail)
        return self._version_id(item_id)

    def _dispatch_mock(self, item_id: str) -> str:
        from app.generation.worker import handle_run_item

        with tenant_session(self.ctx) as s:
            handle_run_item(s, self.ctx, item_id)
        return self._version_id(item_id)

    def _version_id(self, item_id: str) -> str:
        with tenant_session(self.ctx) as s:
            return s.execute(
                select(RunItemAssetVersionBindingRecord.asset_version_id)
                .where(RunItemAssetVersionBindingRecord.run_item_id == item_id)
                .order_by(RunItemAssetVersionBindingRecord.created_at.desc(), RunItemAssetVersionBindingRecord.id.desc())
            ).scalars().first()

    def _client(self, script: _Script | None = None) -> LlmChatClient:
        return LlmChatClient(
            coordinator=self.coordinator, session_factory=tenant_session, protector=PROTECTOR,
            adapters={"mock": MockLlmChatAdapter(), "openai": (script or _Script()).adapter()},
            blob_store=self.blob_store,
        )

    def _check(self, version_id: str, item_id: str, client: LlmChatClient | None = None, ctx=None):
        from app.generation.qc import run_quality_check

        return asyncio.run(run_quality_check(
            ctx or self.ctx.for_system_execution(), asset_version_id=version_id, run_item_id=item_id,
            llm_client=client or self._client(), blob_store=self.blob_store, session_factory=tenant_session,
        ))

    def _job_id(self, s, item_id: str) -> str:
        return s.execute(
            select(JobRecord.id).where(JobRecord.resource_type == "run_item", JobRecord.resource_id == item_id)
            .order_by(JobRecord.created_at.desc(), JobRecord.id.desc())
        ).scalars().first()

    def _qc_reservation(self, s, item_id: str) -> SpendReservationRecord:
        key = llm_call_key(self._job_id(s, item_id), QC_CALL_NAME)
        return s.execute(
            select(SpendReservationRecord).where(SpendReservationRecord.semantic_key_hash == key)
        ).scalar_one()

    def _retry(self, run_id: str) -> None:
        from app.generation.recovery_preview import create_recovery_preview
        from app.generation.run_commands import retry_failed_items

        with tenant_session(self.ctx) as s:
            run = s.get(GenerationRunRecord, run_id)
            preview = create_recovery_preview(
                s, self.ctx, run_id, command="retry_failed_items", selection_kind="all_eligible_failed",
                selection_item_ids=[], expected_run_revision=run.revision, idempotency_key=new_id("ik"),
            )
            run = s.get(GenerationRunRecord, run_id)
            retry_failed_items(
                s, self.ctx, run_id=run_id, expected_revision=run.revision,
                preview_id=preview["id"], preview_hash=preview["preview_hash"], idempotency_key=new_id("ik"),
            )

    # -- byok -------------------------------------------------------------

    def test_byok_check_is_reserved_at_admission_sends_image_then_references_and_settles(self):
        self._bind("openai", "OPENAI_API_KEY", b"sk-live")
        product_id, _ = self._product_with_references(
            [(REF_FRONT, "front", 0), (REF_SIDE, "side", 1)], must_match=MUST_MATCH,
        )
        run_id, item_id = self._run(product_id)
        maximum = qc_max_cost("openai", "gpt-5-mini", 3)
        with tenant_session(self.ctx) as s:
            policy = s.get(
                GenerationRecipeSnapshotRecord, s.get(GenerationRunRecord, run_id).recipe_snapshot_id,
            ).canonical_json["quality_check"]
            self.assertEqual((policy["method"], policy["reference_images"]), ("vlm", 2))
            reservation = self._qc_reservation(s, item_id)
            self.assertEqual((reservation.state, reservation.maximum_provider_amount), ("reserved", maximum))

        version_id = self._dispatch_byok(item_id)
        answer = _answer([
            _item("mm-color", "fail", "外壳是亮银色，参考图是哑光黑。", "keep the housing matte black, not silver"),
            _item("mm-logo", "pass"),
        ], overall="fail")
        script = _Script(httpx.Response(200, json=_completion(json.dumps(answer, ensure_ascii=False))))
        outcome = self._check(version_id, item_id, self._client(script))
        self.assertEqual(outcome.state, "failed_blocking")
        self.assertEqual(outcome.projected_review, "needs_changes")

        # One call: the generated image first, then the two references.
        self.assertEqual(len(script.requests), 1)
        body = json.loads(script.requests[0].content)
        self.assertEqual(script.requests[0].headers["authorization"], "Bearer sk-live")
        parts = body["messages"][-1]["content"]
        urls = [part["image_url"]["url"] for part in parts if part.get("type") == "image_url"]
        self.assertEqual(
            [base64.b64decode(url.split(",", 1)[1]) for url in urls], [PNG, REF_FRONT, REF_SIDE],
        )
        prompt = next(part["text"] for part in parts if part.get("type") == "text")
        self.assertIn("[mm-color]", prompt)
        self.assertIn("Matte black housing", prompt)

        with tenant_session(self.ctx) as s:
            reservation = self._qc_reservation(s, item_id)
            self.assertEqual(reservation.state, "settled")
            settled = s.execute(
                select(ProviderCostLedgerEntryRecord).where(
                    ProviderCostLedgerEntryRecord.spend_reservation_id == reservation.id,
                    ProviderCostLedgerEntryRecord.entry_type == "reported_cost",
                )
            ).scalar_one()
            self.assertEqual(
                settled.provider_amount,
                usage_cost(model_spec("openai", "gpt-5-mini"), {
                    "input_tokens": USAGE["prompt_tokens"], "output_tokens": USAGE["completion_tokens"],
                    "cached_input_tokens": USAGE["prompt_tokens_details"]["cached_tokens"],
                }),
            )
            attempt = s.execute(
                select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.asset_version_id == version_id)
            ).scalar_one()
            self.assertEqual((attempt.state, attempt.execution_mode), ("failed_blocking", "byok"))
            llm_attempt = s.get(ProviderCallAttemptRecord, attempt.provider_attempt_id)
            self.assertEqual((llm_attempt.step_kind, llm_attempt.chargeability), ("llm_chat", "charged"))
            self.assertEqual(llm_attempt.spend_reservation_id, reservation.id)
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "failed")
            self.assertIn("外壳是亮银色", item.error_summary)
            # The image is kept.
            self.assertIsNotNone(s.get(AssetVersionRecord, version_id).committed_object_key)
            review = s.execute(
                select(QualityReviewRecord).where(QualityReviewRecord.asset_version_id == version_id)
            ).scalar_one()
            self.assertEqual((review.review_value, review.source), ("needs_changes", "machine_qc"))
            self.assertIn("外壳是亮银色", review.reason)
            audit = s.execute(
                select(AuditEventRecord).where(
                    AuditEventRecord.event_type == "generation.asset_qc_recorded",
                    AuditEventRecord.resource_id == version_id,
                )
            ).scalar_one()
            self.assertEqual(audit.details_json["state"], "failed_blocking")
            self.assertEqual(audit.details_json["failed_rules"], ["must_match:mm-color"])

            from app.generation.asset_queries import get_asset_version_detail
            from app.generation.review import ReviewError, create_review

            detail = get_asset_version_detail(s, self.ctx, version_id)
            self.assertEqual(detail["effective_review_value"], "needs_changes")
            self.assertEqual(detail["reviews"][0]["source"], "machine_qc")
            qc = detail["qc_attempts"][0]
            self.assertEqual(qc["state"], "failed_blocking")
            self.assertEqual(qc["reasons"], ["外壳是亮银色，参考图是哑光黑。"])
            self.assertEqual(qc["consecutive_failures"], 1)
            checks = {check["rule_id"]: check for check in qc["checks"]}
            self.assertEqual(checks["must_match:mm-color"]["verdict"], "fail")
            self.assertEqual(checks["must_match:mm-color"]["fix_hint"], "keep the housing matte black, not silver")
            self.assertEqual(checks["must_match:mm-logo"]["verdict"], "pass")
            # A blocking failure cannot be approved directly.
            with self.assertRaises(ReviewError):
                create_review(s, self.ctx, version_id, "approved", "看着不错", "owner_a",
                              supersedes_review_id=detail["review_head_id"])

            from app.generation.run_queries import get_run_item_detail

            item_detail = get_run_item_detail(s, self.ctx, run_id, item_id)
            self.assertEqual(item_detail["qc_result"]["state"], "failed")
            self.assertIn(
                "外壳是亮银色，参考图是哑光黑。", [check["detail"] for check in item_detail["qc_result"]["checks"]],
            )

    def test_a_check_that_could_not_be_made_is_uncertain_and_releases_its_reservation(self):
        self._bind("openai", "OPENAI_API_KEY", b"sk-live")
        product_id, _ = self._product_with_references([], must_match=MUST_MATCH)
        run_id, item_id = self._run(product_id)
        version_id = self._dispatch_byok(item_id)
        script = _Script(httpx.Response(429, json={"error": {"message": "slow down"}}))
        outcome = self._check(version_id, item_id, self._client(script))
        self.assertEqual(outcome.state, "uncertain")
        with tenant_session(self.ctx) as s:
            self.assertEqual(self._qc_reservation(s, item_id).state, "released")
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "succeeded")
            attempt = s.execute(
                select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.asset_version_id == version_id)
            ).scalar_one()
            self.assertEqual(attempt.state, "uncertain")
            self.assertIn("rate_limited", attempt.feedback_summary_json["reasons"][0])
            # Uncertain is not a failure: no quality is projected.
            self.assertEqual(
                s.execute(select(QualityReviewRecord).where(
                    QualityReviewRecord.asset_version_id == version_id
                )).scalars().all(),
                [],
            )

    def test_an_unreadable_reference_is_uncertain_never_a_silent_pass(self):
        self._bind("openai", "OPENAI_API_KEY", b"sk-live")
        product_id, _ = self._product_with_references([(REF_FRONT, "front", 0)], must_match=MUST_MATCH[:1])
        run_id, item_id = self._run(product_id)
        version_id = self._dispatch_byok(item_id)
        # The reference derivative disappears from the bucket after generation.
        for key in [k for k in self.blob_store.objects if "/reference_asset/" in k]:
            del self.blob_store.objects[key]
        answer = _answer([_item("mm-color", "pass")])
        script = _Script(httpx.Response(200, json=_completion(json.dumps(answer))))
        self.assertEqual(self._check(version_id, item_id, self._client(script)).state, "uncertain")
        body = json.loads(script.requests[0].content)
        images = [p for p in body["messages"][-1]["content"] if p.get("type") == "image_url"]
        self.assertEqual(len(images), 1)  # only the generated image could be sent
        with tenant_session(self.ctx) as s:
            rows = s.execute(
                select(AssetQCCheckResultRecord)
                .join(AssetQCAttemptRecord, AssetQCAttemptRecord.id == AssetQCCheckResultRecord.qc_attempt_id)
                .where(AssetQCAttemptRecord.asset_version_id == version_id)
            ).scalars().all()
            by_rule = {row.rule_id: row for row in rows}
            self.assertEqual(by_rule["must_match:mm-color"].outcome, "error")
            self.assertEqual(by_rule["reference_images"].outcome, "error")

    def test_without_an_openai_key_a_byok_run_reserves_nothing_and_records_why(self):
        self._bind("gemini", "GEMINI_API_KEY", b"g-live")
        product_id, _ = self._product_with_references([], must_match=MUST_MATCH)
        run_id, item_id = self._run(product_id, provider="gemini", model="gemini-3.1-flash-image")
        with tenant_session(self.ctx) as s:
            self.assertEqual(
                s.execute(select(SpendReservationRecord).where(
                    SpendReservationRecord.semantic_key_hash == llm_call_key(self._job_id(s, item_id), QC_CALL_NAME)
                )).scalars().all(),
                [],
            )
            run = s.get(GenerationRunRecord, run_id)
            policy = s.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id).canonical_json["quality_check"]
            self.assertEqual(policy["method"], "unavailable")
            # Maximum cost is the image alone.
            from app.generation import pricing

            self.assertEqual(
                run.est_cost, pricing.unit_price("gemini", "gemini-3.1-flash-image", {"aspect_ratio": "1:1"}),
            )

        from app.generation.byok_dispatch import handle_run_item_byok

        scripted = _ScriptedAdapter(PNG)
        scripted.provider = "gemini"
        asyncio.run(handle_run_item_byok(
            self.coordinator, self.ctx, item_id, adapters={"gemini": scripted},
            blob_store=self.blob_store, protector=PROTECTOR, session_factory=tenant_session,
        ))
        version_id = self._version_id(item_id)
        script = _Script()
        outcome = self._check(version_id, item_id, self._client(script))
        self.assertEqual(outcome.state, "uncertain")
        self.assertEqual(script.requests, [])
        with tenant_session(self.ctx) as s:
            attempt = s.execute(
                select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.asset_version_id == version_id)
            ).scalar_one()
            self.assertIn("OpenAI", attempt.feedback_summary_json["reasons"][0])

    # -- mock, three strikes, feedback -----------------------------------

    def test_mock_check_is_free_and_passes_by_default(self):
        product_id, _ = self._product_with_references([], must_match=MUST_MATCH)
        run_id, item_id = self._run(product_id, mode="mock", provider="mock", model="mock-v1")
        version_id = self._dispatch_mock(item_id)
        outcome = self._check(version_id, item_id)
        self.assertEqual(outcome.state, "passed")
        with tenant_session(self.ctx) as s:
            attempt = s.execute(
                select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.asset_version_id == version_id)
            ).scalar_one()
            self.assertEqual(attempt.execution_mode, "mock")
            self.assertTrue(attempt.feedback_summary_json["controlled_mock"])
            llm_attempt = s.get(ProviderCallAttemptRecord, attempt.provider_attempt_id)
            self.assertEqual((llm_attempt.chargeability, llm_attempt.spend_reservation_id), ("free_mock", None))
            # Nothing real is reserved for a Mock check.
            self.assertEqual(
                s.execute(select(SpendReservationRecord).where(
                    SpendReservationRecord.semantic_key_hash == llm_call_key(self._job_id(s, item_id), QC_CALL_NAME)
                )).scalars().all(),
                [],
            )
            self.assertEqual(s.get(RunItemRecord, item_id).state, "succeeded")
            self.assertEqual(s.get(GenerationRunRecord, run_id).status, "succeeded")

    def test_three_consecutive_failures_keep_everything_and_project_rejected(self):
        failing = [dict(MUST_MATCH[0], description="Matte black housing [mock:qc-fail]")]
        product_id, _ = self._product_with_references([], must_match=failing)
        run_id, item_id = self._run(product_id, mode="mock", provider="mock", model="mock-v1")

        versions = []
        outcomes = []
        for round_ in range(3):
            if round_:
                self._retry(run_id)
                with tenant_session(self.ctx) as s:
                    item = s.get(RunItemRecord, item_id)
                    recipe = build_creative_recipe(s, self.ctx, item, s.get(GenerationRunRecord, run_id))
                    # The retry's recipe carries what the last check found wrong.
                    self.assertEqual(recipe.qc_feedback, ("match the reference exactly: Matte black housing",))
                    self.assertIn(
                        "Corrections from the previous quality check: match the reference exactly",
                        compose_text_prompt(recipe),
                    )
            version_id = self._dispatch_mock(item_id)
            versions.append(version_id)
            outcomes.append(self._check(version_id, item_id))

        self.assertEqual([o.state for o in outcomes], ["failed_blocking"] * 3)
        self.assertEqual([o.consecutive_failures for o in outcomes], [1, 2, 3])
        self.assertEqual([o.projected_review for o in outcomes], ["needs_changes", "needs_changes", "rejected"])
        self.assertEqual(len(set(versions)), 3)
        with tenant_session(self.ctx) as s:
            from app.generation.review import project_effective_review

            self.assertEqual(
                [project_effective_review(s, self.ctx, v) for v in versions],
                ["needs_changes", "needs_changes", "rejected"],
            )
            review = s.execute(
                select(QualityReviewRecord).where(QualityReviewRecord.asset_version_id == versions[-1])
            ).scalar_one()
            self.assertIn("连续 3 次自动质检未通过", review.reason)
            # Artifacts and evidence of every round are kept.
            attempts = s.execute(
                select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.run_item_id == item_id)
                .order_by(AssetQCAttemptRecord.created_at)
            ).scalars().all()
            self.assertEqual([a.feedback_round for a in attempts], [0, 1, 2])
            self.assertEqual(len(versions), len({s.get(AssetVersionRecord, v).id for v in versions}))
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "failed")
            self.assertIn("qc_blocking_failure", item.error_summary)

    def test_regeneration_carries_the_reasons_into_the_next_recipe(self):
        failing = [dict(MUST_MATCH[0], description="Matte black housing [mock:qc-fail]")]
        product_id, _ = self._product_with_references([], must_match=failing)
        _run_id, item_id = self._run(product_id, mode="mock", provider="mock", model="mock-v1")
        source = self._dispatch_mock(item_id)
        self.assertEqual(self._check(source, item_id).state, "failed_blocking")

        from app.generation.preview import create_preview
        from app.generation.regeneration import create_regeneration_draft
        from app.generation.submission import submit_generation

        with tenant_session(self.ctx) as s:
            draft = create_regeneration_draft(s, self.ctx, source, "quality_feedback")
            feedback = draft.selectors_json["qc_feedback"]
            self.assertEqual(feedback[0]["hint"], "match the reference exactly: Matte black housing")
            self.assertIn("受控 Mock", feedback[0]["reason"])
            self.assertEqual(feedback[0]["source_asset_version_id"], source)
            preview = create_preview(s, self.ctx, draft.id, draft.revision)
            self.assertEqual(
                [entry["hint"] for entry in preview.snapshot_json["recipe"]["creative"]["qc_feedback"]],
                ["match the reference exactly: Matte black housing"],
            )
            run_id = submit_generation(
                s, self.ctx, preview_id=preview.id, preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision, idempotency_key=new_id("ik"),
            )["generation_run_id"]
            new_item = self._item(s, run_id)
            recipe = build_creative_recipe(s, self.ctx, new_item, s.get(GenerationRunRecord, run_id))
            self.assertEqual(recipe.qc_feedback, ("match the reference exactly: Matte black housing",))
            new_item_id = new_item.id

        regenerated = self._dispatch_mock(new_item_id)
        outcome = self._check(regenerated, new_item_id)
        # The regeneration is the same combination's next attempt.
        self.assertEqual(outcome.consecutive_failures, 2)

    def test_mock_scenarios_are_controllable_from_data(self):
        uncertain = [dict(MUST_MATCH[0], description="Matte black housing [mock:qc-uncertain]")]
        product_id, _ = self._product_with_references([], must_match=uncertain)
        _run_id, item_id = self._run(product_id, mode="mock", provider="mock", model="mock-v1")
        version_id = self._dispatch_mock(item_id)
        self.assertEqual(self._check(version_id, item_id).state, "uncertain")

    # -- Issue #194: composition checks and v1 replay ---------------------

    def test_a_composite_image_fails_the_item_blocks_approval_and_feeds_the_retry(self):
        product_id, _ = self._product_with_references(
            [], must_match=[dict(MUST_MATCH[0], description="Matte black housing [mock:qc-composite]")],
        )
        run_id, item_id = self._run(product_id, mode="mock", provider="mock", model="mock-v1")
        version_id = self._dispatch_mock(item_id)
        outcome = self._check(version_id, item_id)
        self.assertEqual(outcome.state, "failed_blocking")
        self.assertEqual(outcome.projected_review, "needs_changes")
        with tenant_session(self.ctx) as s:
            attempt = s.execute(
                select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.asset_version_id == version_id)
            ).scalar_one()
            self.assertEqual(
                (attempt.qc_policy_version, attempt.rule_set_version), ("vlm-qc/v2", "vlm-qc-rules/v2"),
            )
            self.assertEqual(attempt.feedback_summary_json["controlled_mock"], True)
            rows = {
                row.rule_id: row for row in s.execute(
                    select(AssetQCCheckResultRecord).where(AssetQCCheckResultRecord.qc_attempt_id == attempt.id)
                ).scalars()
            }
            self.assertEqual(rows["composition:single_image"].outcome, "fail")
            self.assertEqual(rows["composition:single_image"].severity, "must_match")
            self.assertEqual(rows["must_match:mm-color"].outcome, "pass")
            self.assertEqual(rows["composition:product_once"].outcome, "pass")
            item = s.get(RunItemRecord, item_id)
            self.assertEqual(item.state, "failed")
            self.assertIn("竖直接缝", item.error_summary)

            from app.generation.asset_queries import get_asset_version_detail
            from app.generation.review import ReviewError, create_review

            detail = get_asset_version_detail(s, self.ctx, version_id)
            self.assertEqual(detail["effective_review_value"], "needs_changes")
            checks = {check["rule_id"]: check for check in detail["qc_attempts"][0]["checks"]}
            self.assertIn("单一连贯画面", checks["composition:single_image"]["description"])
            with self.assertRaises(ReviewError):
                create_review(s, self.ctx, version_id, "approved", "看着不错", "owner_a",
                              supersedes_review_id=detail["review_head_id"])

        self._retry(run_id)
        with tenant_session(self.ctx) as s:
            item = s.get(RunItemRecord, item_id)
            recipe = build_creative_recipe(s, self.ctx, item, s.get(GenerationRunRecord, run_id))
            self.assertEqual(len(recipe.qc_feedback), 1)
            self.assertIn("no split screen, no collage, no side-by-side panels", recipe.qc_feedback[0])
            self.assertIn("Corrections from the previous quality check: render one single", compose_text_prompt(recipe))

    def _freeze_v1(self):
        """Previews freeze a ``vlm-qc/v1`` policy, as before Issue #194."""
        import functools
        from unittest import mock

        return mock.patch(
            "app.generation.preview.resolve_qc_policy",
            functools.partial(resolve_qc_policy, version=QC_POLICY_VERSION_V1),
        )

    def test_a_byok_recipe_frozen_under_v1_replays_at_the_v1_price_prompt_and_mapping(self):
        self._bind("openai", "OPENAI_API_KEY", b"sk-live")
        product_id, _ = self._product_with_references(
            [(REF_FRONT, "front", 0), (REF_SIDE, "side", 1)], must_match=MUST_MATCH,
        )
        with self._freeze_v1():
            run_id, item_id = self._run(product_id)
        with tenant_session(self.ctx) as s:
            run = s.get(GenerationRunRecord, run_id)
            policy = s.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id).canonical_json["quality_check"]
            self.assertEqual(policy["version"], "vlm-qc/v1")
            self.assertNotIn("composition_checks", policy)
            self.assertEqual(self._qc_reservation(s, item_id).maximum_provider_amount, V1_MAX_COST[3])

        version_id = self._dispatch_byok(item_id)
        answer = _answer_v1([
            _item("mm-color", "fail", "外壳是亮银色。", "keep the housing matte black"), _item("mm-logo", "pass"),
        ], overall="fail")
        script = _Script(httpx.Response(200, json=_completion(json.dumps(answer, ensure_ascii=False))))
        # The v1 request fits the v1 reservation (no reservation_insufficient).
        self.assertEqual(self._check(version_id, item_id, self._client(script)).state, "failed_blocking")
        body = json.loads(script.requests[0].content)
        self.assertEqual(body["messages"][0]["content"], QC_SYSTEM_PROMPT_V1)
        self.assertNotIn("composition", json.dumps(body["response_format"]))
        with tenant_session(self.ctx) as s:
            self.assertEqual(self._qc_reservation(s, item_id).state, "settled")
            attempt = s.execute(
                select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.asset_version_id == version_id)
            ).scalar_one()
            self.assertEqual(
                (attempt.qc_policy_version, attempt.rule_set_version), ("vlm-qc/v1", "vlm-qc-rules/v1"),
            )
            rules = set(s.execute(
                select(AssetQCCheckResultRecord.rule_id).where(AssetQCCheckResultRecord.qc_attempt_id == attempt.id)
            ).scalars())
            self.assertIn("text_artifacts", rules)
            self.assertFalse(any(rule.startswith("composition:") for rule in rules))

        # A Recovery retry of the v1 recipe reserves v1's maximum again (the
        # Worker builds that retry's check from the same frozen v1 policy).
        self._retry(run_id)
        with tenant_session(self.ctx) as s:
            reservation = self._qc_reservation(s, item_id)
            self.assertEqual((reservation.state, reservation.maximum_provider_amount), ("reserved", V1_MAX_COST[3]))

    def test_a_mock_recipe_frozen_under_v1_ignores_the_composite_marker(self):
        product_id, _ = self._product_with_references(
            [], must_match=[dict(MUST_MATCH[0], description="Matte black housing [mock:qc-composite]")],
        )
        with self._freeze_v1():
            _run_id, item_id = self._run(product_id, mode="mock", provider="mock", model="mock-v1")
        version_id = self._dispatch_mock(item_id)
        # v1 has no composition checks: the same answer as before Issue #194.
        self.assertEqual(self._check(version_id, item_id).state, "passed")
        with tenant_session(self.ctx) as s:
            attempt = s.execute(
                select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.asset_version_id == version_id)
            ).scalar_one()
            self.assertEqual(attempt.qc_policy_version, "vlm-qc/v1")

    def test_a_redelivered_job_does_not_check_twice(self):
        product_id, _ = self._product_with_references([], must_match=MUST_MATCH)
        _run_id, item_id = self._run(product_id, mode="mock", provider="mock", model="mock-v1")
        version_id = self._dispatch_mock(item_id)
        first = self._check(version_id, item_id)
        again = self._check(version_id, item_id)
        self.assertTrue(again.replayed)
        with tenant_session(self.ctx) as s:
            self.assertEqual(
                len(s.execute(select(AssetQCAttemptRecord).where(
                    AssetQCAttemptRecord.asset_version_id == version_id
                )).scalars().all()),
                1,
            )
        self.assertEqual(first.state, "passed")

    # -- tenancy -----------------------------------------------------------

    def test_another_workspace_cannot_check_this_version_or_read_its_references(self):
        self._bind("openai", "OPENAI_API_KEY", b"sk-live")
        product_id, _ = self._product_with_references([(REF_FRONT, "front", 0)], must_match=MUST_MATCH[:1])
        run_id, item_id = self._run(product_id)
        version_id = self._dispatch_byok(item_id)
        reads_before = list(self.blob_store.reads)

        other = self.owner_ctx(self._make_context(role="owner"))
        with self.assertRaises(NoResultFound):
            self._check(version_id, item_id, ctx=other.for_system_execution())
        self.assertEqual(self.blob_store.reads, reads_before)

        # A tampered snapshot naming another Workspace's reference blob: the
        # check never reads it, and the verdict cannot be a pass.
        foreign_key = f"committed/{other.workspace_id}/reference_asset/{'f' * 64}.jpg"
        self.blob_store.objects[foreign_key] = REF_SIDE
        with tenant_session(self.ctx) as s:
            run = s.get(GenerationRunRecord, run_id)
            snapshot = s.get(GenerationRecipeSnapshotRecord, run.recipe_snapshot_id)
            tampered = copy.deepcopy(snapshot.canonical_json)
            tampered["creative"]["reference_images"][0]["committed_key"] = foreign_key
            snapshot.canonical_json = tampered
        script = _Script(httpx.Response(200, json=_completion(json.dumps(_answer([_item("mm-color", "pass")])))))
        self.assertEqual(self._check(version_id, item_id, self._client(script)).state, "uncertain")
        self.assertNotIn(foreign_key, self.blob_store.reads)
        body = json.loads(script.requests[0].content)
        self.assertEqual(len([p for p in body["messages"][-1]["content"] if p.get("type") == "image_url"]), 1)



class WorkerQualityCheckTests(LibraryFixtures):
    """The ``generation.qc_asset_version`` Job runs the governed check."""

    def setUp(self) -> None:
        self.ctx = self._make_context()
        self.addCleanup(cancel_workspace_jobs, self.ctx.workspace_id)

    def test_a_mock_run_drained_by_the_worker_is_checked_by_the_controlled_mock(self):
        import tempfile

        from app.adapters.blob_store_local import LocalFileBlobStore
        from app.generation.draft import create_draft
        from app.generation.preview import create_preview
        from app.generation.submission import submit_generation
        from app.worker_runtime import WorkerRuntime

        with tenant_session(self.ctx) as s:
            draft = create_draft(
                s, self.ctx, product_id=self._product_id(self.ctx, s), provider="mock", model="mock-v1",
                execution_mode="mock", combination_strategy={"kind": "full", "planner_version": "v1"},
            )
            preview = create_preview(s, self.ctx, draft.id, draft.revision)
            run_id = submit_generation(
                s, self.ctx, preview_id=preview.id, preview_hash=preview.preview_hash,
                expected_draft_revision=draft.revision, idempotency_key=new_id("ik"),
            )["generation_run_id"]

        with tempfile.TemporaryDirectory() as root:
            runtime = WorkerRuntime(owner="vlm-qc-worker")
            runtime.blob_store = LocalFileBlobStore(
                root=root, public_base_url="http://blob.test", signing_secret=b"test-secret",
            )
            quarantine_foreign_jobs(self.ctx.workspace_id)
            while asyncio.run(runtime.run_once()):
                pass

        with tenant_session(self.ctx) as s:
            self.assertEqual(s.get(GenerationRunRecord, run_id).status, "succeeded")
            items = s.execute(select(RunItemRecord).where(RunItemRecord.generation_run_id == run_id)).scalars().all()
            for item in items:
                self.assertEqual(item.state, "succeeded")
                attempt = s.execute(
                    select(AssetQCAttemptRecord).where(AssetQCAttemptRecord.run_item_id == item.id)
                ).scalar_one()
                self.assertEqual((attempt.state, attempt.qc_policy_version), ("passed", "vlm-qc/v2"))
                self.assertEqual(attempt.rule_set_version, "vlm-qc-rules/v2")
                llm_attempt = s.get(ProviderCallAttemptRecord, attempt.provider_attempt_id)
                self.assertEqual((llm_attempt.step_kind, llm_attempt.provider), ("llm_chat", "mock"))
                checks = s.execute(
                    select(AssetQCCheckResultRecord).where(AssetQCCheckResultRecord.qc_attempt_id == attempt.id)
                ).scalars().all()
                self.assertEqual({c.check_type for c in checks}, {"vlm"})
                job = s.execute(
                    select(JobRecord).where(
                        JobRecord.type == "generation.qc_asset_version",
                        JobRecord.resource_id == attempt.asset_version_id,
                    )
                ).scalar_one()
                self.assertEqual(job.state, "succeeded")


if __name__ == "__main__":
    unittest.main()
